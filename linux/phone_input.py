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

Injection runs on its own thread (InputWorker), never on a phone's video I/O
thread: on GNOME every touch is a D-Bus round trip answered by gnome-shell's
main loop, which is busiest exactly while a touch drag makes it redraw. Queued
MOVEs of a finger are coalesced to its latest position, DOWN/UP keep their
order, so a slow compositor costs touch smoothness, never video.
"""

import json
import struct
import sys
import threading
from collections import deque

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
    try:
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass                    # stderr gone (terminal closed): never raise


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
        # Free every slot first, then lift: one failed up() (uinput error)
        # must not leak the phone's other slots or skip their lifts.
        keys = [k for k in self._slot_of if k[0] is client]
        slots = [self._slot_of[k] for k in keys]
        for key in keys:
            self._release_slot(key)
        error = None
        for slot in slots:
            try:
                self.surface.up(slot)
            except Exception as e:
                error = e
        if error is not None:
            raise error             # the input worker logs it

    def _release_slot(self, key):
        self._free_slots.append(self._slot_of.pop(key))
        self._free_slots.sort()


class InputWorker:
    """Feeds a TouchRouter from one dedicated thread through a small queue.

    submit() never blocks on the compositor. A MOVE replaces the finger's
    still-queued MOVE (only the latest position matters); DOWN / UP / CANCEL
    are kept in order. If the compositor stalls for long enough that the queue
    still fills up, the queued events are dropped and the affected phones'
    fingers lifted, so no finger is left stuck down.
    """

    MAX_QUEUED = 256
    _GONE = -1                  # internal action: the phone disconnected

    def __init__(self, router):
        self._router = router
        self._queue = deque()             # [client, action, finger, x, y]
        self._queued_move = {}            # (client, finger) -> its queued MOVE
        self._cond = threading.Condition()
        self._closed = False
        self._errors = 0
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="touch-input")
        self._thread.start()

    def submit(self, client, action, finger, x=0.0, y=0.0):
        with self._cond:
            if self._closed:
                return
            key = (client, finger)
            if action == TOUCH_MOVE:
                queued = self._queued_move.get(key)
                if queued is not None:
                    queued[3], queued[4] = x, y
                    return
            elif action in (TOUCH_CANCEL, self._GONE):
                for k in [k for k in self._queued_move if k[0] is client]:
                    del self._queued_move[k]
            else:
                self._queued_move.pop(key, None)  # later MOVEs go after this
            if len(self._queue) >= self.MAX_QUEUED:
                self._overflow_locked()
            event = [client, action, finger, x, y]
            self._queue.append(event)
            if action == TOUCH_MOVE:
                self._queued_move[key] = event
            self._cond.notify()

    def _overflow_locked(self):
        clients = {e[0] for e in self._queue}
        self._queue.clear()
        self._queued_move.clear()
        for c in clients:                 # lift what those phones held down
            self._queue.append([c, TOUCH_CANCEL, 0, 0.0, 0.0])
        log("warning: touch input is backed up (compositor not answering); "
            "dropped queued touches")

    def close(self, timeout=1.0):
        with self._cond:
            self._closed = True
            self._cond.notify()
        self._thread.join(timeout)

    def _run(self):
        while True:
            with self._cond:
                while not self._queue and not self._closed:
                    self._cond.wait()
                if self._closed:
                    return
                event = self._queue.popleft()
                client, action, finger, x, y = event
                if self._queued_move.get((client, finger)) is event:
                    del self._queued_move[(client, finger)]
            try:
                if action == self._GONE:
                    self._router.client_gone(client)
                else:
                    self._router.handle(client, action, finger, x, y)
            except Exception as e:
                # e.g. a uinput write error: log it, keep serving touches
                self._errors += 1
                if self._errors <= 3:
                    log(f"warning: touch injection failed ({e!r})")


class PhoneInput:
    """Handles what admitted phones send: touch events.

    touch_surface: the backend's virtual touchscreen, or None when touch isn't
    available (then `unavailable_reason` says why, and phones are told so).
    """

    def __init__(self, touch_surface=None, unavailable_reason=""):
        self.unavailable_reason = unavailable_reason
        self.router = (TouchRouter(touch_surface, touch_surface.slot_count)
                       if touch_surface else None)
        self._worker = InputWorker(self.router) if self.router else None
        self._authorized = set()      # clients allowed to send touches
        self._lock = threading.Lock()

    def on_admitted(self, client):
        """A phone passed the pairing handshake: tell it whether it may touch."""
        allowed = self.router is not None
        reason = "" if allowed else (self.unavailable_reason
                                     or "touch is off on the computer")
        if allowed:
            with self._lock:
                # The phone may already be gone (its I/O thread runs from the
                # start, so on_disconnect can come first): don't keep it.
                if client.alive:
                    self._authorized.add(client)
        log(f"client {client.addr[0]}: touch "
            + ("enabled" if allowed else f"disabled ({reason})"))
        client.send_message(FRAME_INPUT_STATE, json.dumps(
            {"touch": allowed, "reason": reason}).encode())

    def on_message(self, client, frame_type, body):
        """Called by a client's I/O thread for every frame it receives.
        Never blocks: touches are queued for the input worker."""
        if frame_type == FRAME_TOUCH:
            self._on_touch(client, body)
        # unknown types are ignored, so newer apps keep working

    def on_disconnect(self, client):
        with self._lock:
            self._authorized.discard(client)
        if self._worker:
            # Through the queue, so it lands after the phone's queued touches.
            self._worker.submit(client, InputWorker._GONE, 0)

    def close(self):
        """Stop the input worker (before the touch surface is closed)."""
        if self._worker:
            self._worker.close()

    def _on_touch(self, client, body):
        if self.router is None or len(body) != TOUCH_BODY.size:
            return
        with self._lock:
            if client not in self._authorized:
                return
        action, finger, raw_x, raw_y = TOUCH_BODY.unpack(body)
        self._worker.submit(client, action, finger,
                            raw_x / COORD_SCALE, raw_y / COORD_SCALE)
