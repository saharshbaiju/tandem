"""
uinput_pointer.py - a virtual absolute mouse through /dev/uinput (Hyprland).

Used for the phone's pointer mode (see phone_input.py), where the phone acts
as a mouse rather than a touchscreen, because many Linux apps only drag with
a mouse: Chrome's tabs, window title bars, text selection.

The device reports absolute positions (like a VM's "tablet" mouse) over the
whole desktop layout, the box around every monitor. The compositor maps such
a device onto that box, so this module turns a fraction of the phone's
monitor into a position in the box, from `geometry()`:

    geometry() -> ((box_x, box_y, box_w, box_h), (out_x, out_y, out_w, out_h))

in layout (logical) pixels. Unlike the touchscreen, it moves the computer's
own mouse pointer; that is what pointer mode is for.

Buttons are evdev codes (BTN_LEFT...). Scrolling is in 1/120 of a wheel
notch, the high-resolution wheel unit, so two-finger scrolling is smooth.
"""

import fcntl
import os
import struct
import threading
import time

import uinput_touch as ui

EV_REL = 0x02
ABS_X, ABS_Y = 0x00, 0x01
REL_HWHEEL, REL_WHEEL = 0x06, 0x08
REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES = 0x0B, 0x0C
BTN_LEFT, BTN_RIGHT, BTN_MIDDLE = 0x110, 0x111, 0x112
UI_SET_RELBIT = 0x40045566

ABS_MAX = 65535                 # device resolution over the layout box
NOTCH = 120                     # hi-res wheel units per wheel notch
POINTER_DEVICE_NAME = "tandem-pointer"


class VirtualPointer:
    """An absolute mouse whose range spans the layout box from `geometry`."""

    def __init__(self, geometry, name=POINTER_DEVICE_NAME):
        self.description = f"uinput absolute pointer '{name}'"
        self._geometry = geometry
        self._lock = threading.Lock()
        self._buttons = set()
        self._wheel_rest = [0, 0]       # hi-res units not yet a whole notch
        self._fd = os.open(ui.UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        try:
            self._configure(name)
        except OSError:
            os.close(self._fd)
            raise

    def _configure(self, name):
        fd = self._fd
        fcntl.ioctl(fd, ui.UI_SET_EVBIT, ui.EV_KEY)
        for button in (BTN_LEFT, BTN_RIGHT, BTN_MIDDLE):
            fcntl.ioctl(fd, ui.UI_SET_KEYBIT, button)
        fcntl.ioctl(fd, ui.UI_SET_EVBIT, EV_REL)
        for axis in (REL_WHEEL, REL_HWHEEL, REL_WHEEL_HI_RES, REL_HWHEEL_HI_RES):
            fcntl.ioctl(fd, UI_SET_RELBIT, axis)
        fcntl.ioctl(fd, ui.UI_SET_EVBIT, ui.EV_ABS)
        for axis in (ABS_X, ABS_Y):
            fcntl.ioctl(fd, ui.UI_SET_ABSBIT, axis)
            fcntl.ioctl(fd, ui.UI_ABS_SETUP,
                        struct.pack("Hxxiiiiii", axis, 0, 0, ABS_MAX, 0, 0, 0))
        # struct uinput_setup, as in uinput_touch (its own product id)
        fcntl.ioctl(fd, ui.UI_DEV_SETUP,
                    struct.pack("HHHH80sI", ui.BUS_VIRTUAL, 0x1209, 0xA5D3, 1,
                                name.encode()[:79], 0))
        fcntl.ioctl(fd, ui.UI_DEV_CREATE)
        time.sleep(0.2)               # let the compositor pick the device up

    # -- the pointer surface (phone_input.PointerRouter) ------------------- #

    def move(self, x, y):
        with self._lock:
            if self._fd is not None:
                self._emit(self._position(x, y))

    def button(self, code, pressed):
        with self._lock:
            if self._fd is None:
                return
            if pressed:
                self._buttons.add(code)
            elif code in self._buttons:
                self._buttons.discard(code)
            else:
                return
            self._emit([(ui.EV_KEY, code, int(pressed))])

    def scroll(self, dx, dy):
        """dx / dy in 1/120 notches; positive dy scrolls down, dx right."""
        with self._lock:
            if self._fd is None:
                return
            events = []
            # evdev: wheel up is positive, so content scrolls down on -dy
            for axis, hi_res, value, i in ((REL_WHEEL, REL_WHEEL_HI_RES, -dy, 1),
                                           (REL_HWHEEL, REL_HWHEEL_HI_RES, dx, 0)):
                if not value:
                    continue
                events.append((EV_REL, hi_res, value))
                self._wheel_rest[i] += value
                notches = int(self._wheel_rest[i] / NOTCH)
                if notches:
                    self._wheel_rest[i] -= notches * NOTCH
                    events.append((EV_REL, axis, notches))
            if events:
                self._emit(events)

    def release_all(self):
        for code in sorted(self._buttons):
            self.button(code, False)

    def close(self):
        with self._lock:
            if self._fd is None:
                return
            try:
                for code in sorted(self._buttons):
                    self._emit([(ui.EV_KEY, code, 0)])
                fcntl.ioctl(self._fd, ui.UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None

    # -- internals ---------------------------------------------------------- #

    def _position(self, x, y):
        (bx, by, bw, bh), (ox, oy, ow, oh) = self._geometry()
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        gx = ox + x * max(ow - 1, 0)
        gy = oy + y * max(oh - 1, 0)
        return [(ui.EV_ABS, ABS_X, _scaled(gx - bx, bw)),
                (ui.EV_ABS, ABS_Y, _scaled(gy - by, bh))]

    def _emit(self, events):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        packed = b"".join(ui.INPUT_EVENT.pack(sec, usec, t, c, v)
                          for t, c, v in events)
        packed += ui.INPUT_EVENT.pack(sec, usec, ui.EV_SYN, ui.SYN_REPORT, 0)
        os.write(self._fd, packed)


def _scaled(offset, span):
    """A layout offset as device units: the box's last pixel is ABS_MAX."""
    if span <= 1:
        return 0
    return min(max(round(offset * ABS_MAX / (span - 1)), 0), ABS_MAX)
