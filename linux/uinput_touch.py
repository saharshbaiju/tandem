"""
uinput_touch.py - a virtual multi-touch *touchscreen* created through /dev/uinput.

Used by the Hyprland backend to replay the phone's touches on the virtual
monitor. A touchscreen (not a mouse) is deliberate:

  * Touch input goes straight to the surface under the finger, so the main
    mouse cursor never moves — you can keep using the laptop's touchpad while
    someone taps on the phone.
  * The compositor maps an absolute touch device onto one output, so the
    device's coordinate range covers exactly the phone's monitor
    (Hyprland: `hl.device({ name = "tandem-touch", output = "EXT-1" })`).

Pure Python (ioctl + struct), no python-evdev dependency. Speaks the kernel's
multi-touch protocol "type B" (slots), the same one real touchscreens use:

    finger down : ABS_MT_SLOT s, ABS_MT_TRACKING_ID id, ABS_MT_POSITION_X/Y
    finger move : ABS_MT_SLOT s, ABS_MT_POSITION_X/Y
    finger up   : ABS_MT_SLOT s, ABS_MT_TRACKING_ID -1
    ...each batch closed by SYN_REPORT.

BTN_TOUCH and ABS_X/ABS_Y (the "single-touch" view of the first finger) are
kept in sync too, because libinput refuses touchscreens without them.

Opening /dev/uinput needs write access: root, the `input` group on Arch, or
the udev rule the installer adds (KERNEL=="uinput", TAG+="uaccess").
"""

import fcntl
import os
import struct
import time

# --- kernel constants (linux/input-event-codes.h, linux/uinput.h) ---------- #

EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT = 0
BTN_TOUCH = 0x14A
ABS_X, ABS_Y = 0x00, 0x01
ABS_MT_SLOT = 0x2F
ABS_MT_POSITION_X = 0x35
ABS_MT_POSITION_Y = 0x36
ABS_MT_TRACKING_ID = 0x39
INPUT_PROP_DIRECT = 0x01          # "touches land where they are": a screen
BUS_VIRTUAL = 0x06

# ioctl numbers: _IOW('U', nr, size) = (1 << 30) | (size << 16) | ('U' << 8) | nr
UI_DEV_CREATE = 0x5501
UI_DEV_DESTROY = 0x5502
UI_DEV_SETUP = 0x405C5503         # struct uinput_setup     (92 bytes)
UI_ABS_SETUP = 0x401C5504         # struct uinput_abs_setup (28 bytes)
UI_SET_EVBIT = 0x40045564
UI_SET_KEYBIT = 0x40045565
UI_SET_ABSBIT = 0x40045567
UI_SET_PROPBIT = 0x4004556E

INPUT_EVENT = struct.Struct("llHHi")   # timeval, type, code, value

UINPUT_PATH = "/dev/uinput"
DEVICE_NAME = "tandem-touch"        # Hyprland lowercases names; keep it simple
MAX_SLOTS = 10                    # simultaneous fingers


def uinput_access_problem():
    """None if we can create input devices, else a one-line reason."""
    if not os.path.exists(UINPUT_PATH):
        return "/dev/uinput is missing (run: sudo modprobe uinput)"
    if not os.access(UINPUT_PATH, os.W_OK):
        return "no permission to use /dev/uinput"
    return None


class VirtualTouchscreen:
    """A kernel touchscreen whose coordinates span `width` x `height` pixels.

    Coordinates passed to down()/move() are fractions of the screen (0..1), so
    callers never care about the device's internal resolution.
    """

    slot_count = MAX_SLOTS

    def __init__(self, width, height, name=DEVICE_NAME):
        self.max_x = max(1, width - 1)
        self.max_y = max(1, height - 1)
        self.description = f"uinput touchscreen '{name}'"
        self._fd = os.open(UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        self._active_slots = set()
        self._next_tracking_id = 1
        try:
            self._configure(name)
        except OSError:
            os.close(self._fd)
            raise

    def _configure(self, name):
        fd = self._fd
        fcntl.ioctl(fd, UI_SET_PROPBIT, INPUT_PROP_DIRECT)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_KEY)
        fcntl.ioctl(fd, UI_SET_KEYBIT, BTN_TOUCH)
        fcntl.ioctl(fd, UI_SET_EVBIT, EV_ABS)
        axes = (
            (ABS_X, 0, self.max_x),
            (ABS_Y, 0, self.max_y),
            (ABS_MT_SLOT, 0, MAX_SLOTS - 1),
            (ABS_MT_TRACKING_ID, 0, 0xFFFF),
            (ABS_MT_POSITION_X, 0, self.max_x),
            (ABS_MT_POSITION_Y, 0, self.max_y),
        )
        for code, minimum, maximum in axes:
            fcntl.ioctl(fd, UI_SET_ABSBIT, code)
            # struct uinput_abs_setup { u16 code; struct input_absinfo {
            #     s32 value, minimum, maximum, fuzz, flat, resolution; } }
            fcntl.ioctl(fd, UI_ABS_SETUP,
                        struct.pack("Hxxiiiiii", code, 0, minimum, maximum,
                                    0, 0, 0))
        # struct uinput_setup { struct input_id { u16 bustype, vendor,
        #     product, version; } char name[80]; u32 ff_effects_max; }
        fcntl.ioctl(fd, UI_DEV_SETUP,
                    struct.pack("HHHH80sI", BUS_VIRTUAL, 0x1209, 0xA5D1, 1,
                                name.encode()[:79], 0))
        fcntl.ioctl(fd, UI_DEV_CREATE)
        # udev and the compositor need a moment to pick up the new device;
        # events written before that are lost.
        time.sleep(0.2)

    # ---- public API: one call = one complete, synced touch update --------- #

    def down(self, slot, x, y):
        if not 0 <= slot < MAX_SLOTS:
            return
        if slot in self._active_slots:          # never left: treat as a move
            self.move(slot, x, y)
            return
        first_finger = not self._active_slots
        self._active_slots.add(slot)
        px, py = self._to_pixels(x, y)
        events = [(EV_ABS, ABS_MT_SLOT, slot),
                  (EV_ABS, ABS_MT_TRACKING_ID, self._new_tracking_id()),
                  (EV_ABS, ABS_MT_POSITION_X, px),
                  (EV_ABS, ABS_MT_POSITION_Y, py)]
        if first_finger:
            events += [(EV_KEY, BTN_TOUCH, 1),
                       (EV_ABS, ABS_X, px), (EV_ABS, ABS_Y, py)]
        self._emit(events)

    def move(self, slot, x, y):
        if slot not in self._active_slots:
            return
        px, py = self._to_pixels(x, y)
        events = [(EV_ABS, ABS_MT_SLOT, slot),
                  (EV_ABS, ABS_MT_POSITION_X, px),
                  (EV_ABS, ABS_MT_POSITION_Y, py)]
        if slot == min(self._active_slots):     # single-touch view follows
            events += [(EV_ABS, ABS_X, px), (EV_ABS, ABS_Y, py)]
        self._emit(events)

    def up(self, slot):
        if slot not in self._active_slots:
            return
        self._active_slots.discard(slot)
        events = [(EV_ABS, ABS_MT_SLOT, slot),
                  (EV_ABS, ABS_MT_TRACKING_ID, -1)]
        if not self._active_slots:
            events.append((EV_KEY, BTN_TOUCH, 0))
        self._emit(events)

    def release_all(self):
        """Lift every finger (a phone vanished mid-gesture)."""
        for slot in sorted(self._active_slots):
            self.up(slot)

    def close(self):
        if self._fd is None:
            return
        try:
            self.release_all()
            fcntl.ioctl(self._fd, UI_DEV_DESTROY)
        except OSError:
            pass
        os.close(self._fd)
        self._fd = None

    # ---- internals -------------------------------------------------------- #

    def _to_pixels(self, x, y):
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        return round(x * self.max_x), round(y * self.max_y)

    def _new_tracking_id(self):
        tracking_id = self._next_tracking_id
        self._next_tracking_id = tracking_id % 0xFFFF + 1
        return tracking_id

    def _emit(self, events):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        packed = b"".join(INPUT_EVENT.pack(sec, usec, etype, code, value)
                          for etype, code, value in events)
        packed += INPUT_EVENT.pack(sec, usec, EV_SYN, SYN_REPORT, 0)
        os.write(self._fd, packed)
