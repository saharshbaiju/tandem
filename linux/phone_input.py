"""
phone_input.py - everything a phone sends *back* to the computer.

The video stream flows PC -> phone. This module handles the reverse direction
on the same (TLS) connection, using the same framing
(4-byte type + 4-byte big-endian length + body):

    INPT  PC -> phone   JSON {"touch": true|false, "reason": "<why not>"}
                        Sent once the phone is admitted: may it send touches?
    TUCH  phone -> PC   6 bytes: action u8, finger u8, x u16, y u16 (big-endian)
                        x/y are fractions of the video picture scaled to
                        0..65535, so they don't depend on either screen's size.

Who may connect at all is decided before a phone gets here: secure_link.py
runs the TLS + pairing-key handshake, and only phones that pass it are handed
to the broadcaster. So every client this module sees has proved it scanned
the QR code; touch only additionally depends on touch being on and a virtual
touchscreen being available.

Touches from every phone go through one TouchRouter, which gives each finger
its own slot on the shared virtual touchscreen and lifts any fingers a phone
still had down when it disconnects (no stuck touches).
"""

import json
import struct
import sys
import threading

FRAME_INPUT_STATE = b"INPT"
FRAME_TOUCH = b"TUCH"

# TUCH action codes (mirrored in the Android app's StreamSession.kt)
TOUCH_DOWN = 0
TOUCH_MOVE = 1
TOUCH_UP = 2
TOUCH_CANCEL = 3          # the gesture was aborted: lift every finger

TOUCH_BODY = struct.Struct(">BBHH")
COORD_SCALE = 65535.0

MAX_INCOMING_FRAME = 4096     # nothing a phone sends is bigger than this


def log(*a):
    print("[tandemd]", *a, file=sys.stderr, flush=True)


class TouchRouter:
    """Shares one virtual touchscreen between every connected phone.

    Each phone numbers its fingers from 0. Two phones touching at once would
    collide, so every (phone, finger) pair is given its own free slot on the
    device. The surface must provide down/move/up(slot, ...) and release_all().
    """

    def __init__(self, surface, slot_count):
        self.surface = surface
        self._free_slots = list(range(slot_count))
        self._slot_of = {}            # (client, finger) -> device slot
        self._lock = threading.Lock()

    def handle(self, client, action, finger, x, y):
        with self._lock:
            if action == TOUCH_CANCEL:
                self._lift_all_of(client)
                return
            key = (client, finger)
            slot = self._slot_of.get(key)
            if action == TOUCH_DOWN:
                if slot is None:
                    if not self._free_slots:
                        return            # more fingers than the device has
                    slot = self._free_slots.pop(0)
                    self._slot_of[key] = slot
                self.surface.down(slot, x, y)
            elif action == TOUCH_MOVE and slot is not None:
                self.surface.move(slot, x, y)
            elif action == TOUCH_UP and slot is not None:
                self.surface.up(slot)
                self._release_slot(key)

    def client_gone(self, client):
        with self._lock:
            self._lift_all_of(client)

    def _lift_all_of(self, client):
        for key in [k for k in self._slot_of if k[0] is client]:
            self.surface.up(self._slot_of[key])
            self._release_slot(key)

    def _release_slot(self, key):
        self._free_slots.append(self._slot_of.pop(key))
        self._free_slots.sort()


class PhoneInput:
    """Handles what admitted phones send: touch events.

    touch_surface: the backend's virtual touchscreen, or None when touch isn't
    available (then `unavailable_reason` says why, and phones are told so).
    """

    def __init__(self, touch_surface=None, unavailable_reason=""):
        self.unavailable_reason = unavailable_reason
        self.router = (TouchRouter(touch_surface, touch_surface.slot_count)
                       if touch_surface else None)
        self._authorized = set()      # clients allowed to send touches
        self._lock = threading.Lock()

    def on_admitted(self, client):
        """A phone passed the pairing handshake: tell it whether it may touch."""
        allowed = self.router is not None
        reason = "" if allowed else (self.unavailable_reason
                                     or "touch is off on the computer")
        if allowed:
            with self._lock:
                self._authorized.add(client)
        log(f"client {client.addr[0]}: touch "
            + ("enabled" if allowed else f"disabled ({reason})"))
        client.send_message(FRAME_INPUT_STATE, json.dumps(
            {"touch": allowed, "reason": reason}).encode())

    def on_message(self, client, frame_type, body):
        """Called by a client's I/O thread for every frame it receives."""
        if frame_type == FRAME_TOUCH:
            self._on_touch(client, body)
        # unknown types are ignored, so newer apps keep working

    def on_disconnect(self, client):
        with self._lock:
            self._authorized.discard(client)
        if self.router:
            self.router.client_gone(client)

    def _on_touch(self, client, body):
        if self.router is None or len(body) != TOUCH_BODY.size:
            return
        with self._lock:
            if client not in self._authorized:
                return
        action, finger, raw_x, raw_y = TOUCH_BODY.unpack(body)
        self.router.handle(client, action, finger,
                           raw_x / COORD_SCALE, raw_y / COORD_SCALE)
