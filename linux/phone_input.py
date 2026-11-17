"""
phone_input.py - everything a phone sends *back* to the computer.

The video stream flows PC -> phone. This module handles the reverse direction
on the same (TLS) connection, using the same framing
(4-byte type + 4-byte big-endian length + body):

    INPT  PC -> phone   JSON {"touch": true|false, "reason": "<why not>",
                              "keys": true|false, "pointer": true|false}
                        Sent once the phone is admitted: may it send touches,
                        can it type, and can it act as a mouse (pointer
                        mode)? Older daemons leave out "keys" / "pointer";
                        the app then offers neither.
    TUCH  phone -> PC   6 bytes: action u8, finger u8, x u16, y u16 (big-endian)
                        x/y are fractions of the video picture scaled to
                        0..65535, so they don't depend on either screen's size.
    KEYS  phone -> PC   5 bytes: keysym u32, pressed u8 (big-endian)
                        An X keysym (keyboard.py says why keysyms) going down
                        (1) or up (0). The phone sends a press and a release
                        per key; key repeat is the computer's job.
    PNTR  phone -> PC   10 bytes: action u8, button u8, x u16, y u16,
                        dx i16, dy i16 (big-endian). Pointer mode, where the
                        phone acts as a mouse: MOVE (0) to x/y, DOWN (1) /
                        UP (2) of a button (1 left, 2 right, 3 middle) at
                        x/y, SCROLL (3) by dx/dy in 1/120 of a wheel notch
                        (positive: right / down). x/y as in TUCH.

Who may connect at all is decided before a phone gets here: secure_link.py
runs the TLS + pairing-key handshake, and only phones that pass it are handed
to the broadcaster. So every client this module sees has proved it scanned
the QR code; touch only additionally depends on touch being on and a virtual
touchscreen being available.

Keys go through a KeyRouter, which remembers what each phone holds down
and lets go of it when the phone disconnects (no stuck Ctrl).

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
FRAME_KEY = b"KEYS"
FRAME_POINTER = b"PNTR"

# PNTR actions and buttons (mirrored in the app's PointerGestures.kt)
POINTER_MOVE, POINTER_DOWN, POINTER_UP, POINTER_SCROLL = 0, 1, 2, 3
POINTER_BUTTONS = {1: 0x110, 2: 0x111, 3: 0x112}   # -> BTN_LEFT/RIGHT/MIDDLE
POINTER_BODY = struct.Struct(">BBHHhh")

# TUCH action codes (mirrored in the Android app's StreamSession.kt)
TOUCH_DOWN = 0
TOUCH_MOVE = 1
TOUCH_UP = 2
TOUCH_CANCEL = 3          # the gesture was aborted: lift every finger

TOUCH_BODY = struct.Struct(">BBHH")
KEY_BODY = struct.Struct(">IB")
MAX_KEYSYM = 0x1FFFFFFF        # X keysyms are 29 bits
COORD_SCALE = 65535.0

MAX_INCOMING_FRAME = 4096     # nothing a phone sends is bigger than this
MAX_FINGERS_PER_CLIENT = 5    # one phone can't take every slot of the shared
#                               touchscreen (a hand has 5; others must still
#                               be able to touch)


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
                    if sum(1 for k in self._slot_of
                           if k[0] is client) >= MAX_FINGERS_PER_CLIENT:
                        return            # this phone already has its share
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


class KeyRouter:
    """Shares one virtual keyboard between every connected phone.

    The keyboard provides key(keysym, pressed); this keeps track of which
    keys each phone holds, so a phone that vanishes mid-shortcut can't leave
    a modifier stuck down for everyone.
    """

    def __init__(self, keyboard):
        self.keyboard = keyboard
        self._held = {}               # client -> set of keysyms held down
        self._lock = threading.Lock()

    def handle(self, client, keysym, pressed):
        with self._lock:
            held = self._held.setdefault(client, set())
            if pressed:
                held.add(keysym)
            elif keysym in held:
                held.discard(keysym)
            else:
                return                # a release we never pressed
            self.keyboard.key(keysym, pressed)

    def client_gone(self, client):
        with self._lock:
            held = self._held.pop(client, set())
            # letters before modifiers, so Ctrl+C can't turn into a bare C
            for keysym in sorted(held, key=_is_modifier):
                self.keyboard.key(keysym, False)


def _is_modifier(keysym):
    return 0xFFE1 <= keysym <= 0xFFEE or keysym == 0xFE03


class PointerRouter:
    """Drives the one virtual pointer for every phone in pointer mode.

    The surface provides move(x, y), button(evdev code, pressed) and
    scroll(dx, dy). Buttons a phone still holds are let go when it
    disconnects, so a vanished phone can't leave a drag hanging.
    """

    def __init__(self, surface):
        self.surface = surface
        self._held = {}               # client -> set of evdev button codes
        self._lock = threading.Lock()

    def handle(self, client, action, button, x, y, dx, dy):
        with self._lock:
            if action == POINTER_SCROLL:
                self.surface.scroll(dx, dy)
                return
            self.surface.move(x, y)
            code = POINTER_BUTTONS.get(button)
            if code is None or action == POINTER_MOVE:
                return
            held = self._held.setdefault(client, set())
            if action == POINTER_DOWN and code not in held:
                held.add(code)
                self.surface.button(code, True)
            elif action == POINTER_UP and code in held:
                held.discard(code)
                self.surface.button(code, False)

    def client_gone(self, client):
        with self._lock:
            for code in sorted(self._held.pop(client, ())):
                self.surface.button(code, False)


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
    _KEY = -2                   # internal action: a key (finger = keysym,
    #                             x = 1.0 pressed / 0.0 released)
    _POINTER = -3               # internal action: a PNTR event (finger =
    #                             (action, button, dx, dy), x/y = position)
    _POINTER_FINGER = -1        # the coalescing key of pointer MOVEs

    def __init__(self, router, keys=None, pointer=None):
        self._router = router
        self._keys = keys
        self._pointer = pointer
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
            if action == self._POINTER and finger[0] == POINTER_MOVE:
                # like a finger's MOVE: only the newest position matters
                key = (client, self._POINTER_FINGER)
                queued = self._queued_move.get(key)
                if queued is not None:
                    queued[3], queued[4] = x, y
                    return
            elif action in (self._KEY, self._POINTER):
                key = None                # never coalesced, kept in order
            elif action == TOUCH_MOVE:
                queued = self._queued_move.get(key)
                if queued is not None:
                    queued[3], queued[4] = x, y
                    return
            elif action in (TOUCH_CANCEL, self._GONE):
                for k in [k for k in self._queued_move if k[0] is client]:
                    del self._queued_move[k]
            elif key is not None:
                self._queued_move.pop(key, None)  # later MOVEs go after this
            if action == self._POINTER and finger[0] != POINTER_MOVE:
                # a click or scroll: a queued pointer MOVE stays before it
                self._queued_move.pop((client, self._POINTER_FINGER), None)
            if len(self._queue) >= self.MAX_QUEUED:
                self._overflow_locked()
            event = [client, action, finger, x, y]
            self._queue.append(event)
            if action == TOUCH_MOVE or (action == self._POINTER
                                        and finger[0] == POINTER_MOVE):
                self._queued_move[key] = event
            self._cond.notify()

    def _overflow_locked(self):
        clients = {e[0] for e in self._queue}
        self._queue.clear()
        self._queued_move.clear()
        for c in clients:                 # lift what those phones held down
            self._queue.append([c, TOUCH_CANCEL, 0, 0.0, 0.0])
            if self._keys or self._pointer:   # dropped releases: let go too
                self._queue.append([c, self._GONE, 0, 0.0, 0.0])
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
                move_key = ((client, self._POINTER_FINGER)
                            if action == self._POINTER else (client, finger))
                if self._queued_move.get(move_key) is event:
                    del self._queued_move[move_key]
            try:
                if action == self._GONE:
                    if self._router:
                        self._router.client_gone(client)
                    if self._keys:
                        self._keys.client_gone(client)
                    if self._pointer:
                        self._pointer.client_gone(client)
                elif action == self._KEY:
                    if self._keys:
                        self._keys.handle(client, finger, x > 0.5)
                elif action == self._POINTER:
                    if self._pointer:
                        kind, button, dx, dy = finger
                        self._pointer.handle(client, kind, button, x, y,
                                             dx, dy)
                elif self._router:
                    self._router.handle(client, action, finger, x, y)
            except Exception as e:
                # e.g. a uinput write error: log it, keep serving touches
                self._errors += 1
                if self._errors <= 3:
                    log(f"warning: touch injection failed ({e!r})")


class PhoneInput:
    """Handles what admitted phones send: touch events and keys.

    touch_surface: the backend's virtual touchscreen, or None when touch isn't
    available (then `unavailable_reason` says why, and phones are told so).
    keyboard: the backend's virtual keyboard, or None.
    pointer: the backend's virtual pointer (pointer mode), or None.
    """

    def __init__(self, touch_surface=None, unavailable_reason="",
                 keyboard=None, pointer=None):
        self.unavailable_reason = unavailable_reason
        self.router = (TouchRouter(touch_surface, touch_surface.slot_count)
                       if touch_surface else None)
        self.keys = KeyRouter(keyboard) if keyboard else None
        self.pointer = PointerRouter(pointer) if pointer else None
        self._worker = (InputWorker(self.router, self.keys, self.pointer)
                        if self.router or self.keys or self.pointer else None)
        self._authorized = set()      # clients allowed to send touches
        self._lock = threading.Lock()

    def on_admitted(self, client):
        """A phone passed the pairing handshake: tell it whether it may touch."""
        allowed = self.router is not None
        typing = self.keys is not None
        mouse = self.pointer is not None
        reason = "" if allowed else (self.unavailable_reason
                                     or "touch is off on the computer")
        if allowed or typing or mouse:
            with self._lock:
                # The phone may already be gone (its I/O thread runs from the
                # start, so on_disconnect can come first): don't keep it.
                if client.alive:
                    self._authorized.add(client)
        log(f"client {client.addr[0]}: touch "
            + ("enabled" if allowed else f"disabled ({reason})"))
        client.send_message(FRAME_INPUT_STATE, json.dumps(
            {"touch": allowed, "reason": reason, "keys": typing,
             "pointer": mouse}).encode())

    def on_message(self, client, frame_type, body):
        """Called by a client's I/O thread for every frame it receives.
        Never blocks: touches are queued for the input worker."""
        if frame_type == FRAME_TOUCH:
            self._on_touch(client, body)
        elif frame_type == FRAME_KEY:
            self._on_key(client, body)
        elif frame_type == FRAME_POINTER:
            self._on_pointer(client, body)
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
        if action > TOUCH_CANCEL:
            return
        self._worker.submit(client, action, finger,
                            raw_x / COORD_SCALE, raw_y / COORD_SCALE)

    def _on_key(self, client, body):
        if self.keys is None or len(body) != KEY_BODY.size:
            return
        with self._lock:
            if client not in self._authorized:
                return
        keysym, pressed = KEY_BODY.unpack(body)
        if 0 < keysym <= MAX_KEYSYM and pressed in (0, 1):
            self._worker.submit(client, InputWorker._KEY, keysym,
                                float(pressed))

    def _on_pointer(self, client, body):
        if self.pointer is None or len(body) != POINTER_BODY.size:
            return
        with self._lock:
            if client not in self._authorized:
                return
        action, button, raw_x, raw_y, dx, dy = POINTER_BODY.unpack(body)
        if action > POINTER_SCROLL:
            return
        self._worker.submit(client, InputWorker._POINTER,
                            (action, button, dx, dy),
                            raw_x / COORD_SCALE, raw_y / COORD_SCALE)
