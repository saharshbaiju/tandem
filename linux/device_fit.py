"""
device_fit.py - size the virtual monitor to the phone or tablet watching it.

With "fit to device" on (tandemd --fit-device, the default in the TUI), the
app reports the size of its screen in a SIZE frame right after pairing, and
again whenever it changes (a foldable unfolds, split screen). The daemon then
gives the virtual monitor that exact size and aspect ratio, so the picture
fills the screen with no black bars, and a touch anywhere on the screen is a
touch on the desktop.

    phone -> PC   SIZE   JSON {"width": W, "height": H[, "now": true]}: the app's screen,
                         in pixels, as it is held: landscape, or portrait
                         (taller than wide) after the user turns the
                         monitor from the app, which then gets a tall
                         monitor

"now" marks a turn the user asked for: it is applied at once, without the
SETTLE wait (a turn is one deliberate change, not a layout settling).

The banner's "fit" field tells the app this is on, so it only offers to
turn the monitor to portrait when the monitor would follow.

Older daemons ignore the frame (unknown frame types are skipped), and with an
older app nothing is sent, so the monitor keeps the size set in Configure.

Rules:
  * Only a phone that is the only one connected resizes the monitor. With
    two or more watching, the monitor stays as it is and the others are
    letterboxed as before.
  * Sizes are capped (MAX_LONG_SIDE, MAX_AREA) and made even, the way
    encoders want them; the aspect ratio is kept.
  * A burst of SIZE frames (the app's layout settling) is applied once,
    SETTLE seconds after the last one.
  * The size last fitted is saved (STATE_PATH) and used when the daemon next
    starts, so the same device reconnecting finds the monitor already right
    and nothing moves.

The resize itself happens in tandemd's capture supervisor, between stopping
the encoder and starting it again (see tandemd.resize_monitor).
"""

import json
import os
import threading
import time

FRAME_SIZE = b"SIZE"

MIN_SIDE = 320                  # smaller is a bogus report, not a screen
MAX_LONG_SIDE = 3840            # what every encoder Tandem uses can take
MAX_AREA = 3840 * 2160
MAX_REPORTED = 16384            # anything bigger in a SIZE frame is garbage
SETTLE = 0.6                    # seconds of quiet before a new size applies

STATE_PATH = os.path.join(os.environ.get("XDG_STATE_HOME")
                          or os.path.expanduser("~/.local/state"),
                          "tandem", "device-size.json")


def parse_size(body):
    """(width, height) from a SIZE frame body, or None if it isn't one."""
    try:
        data = json.loads(body)
        width, height = data["width"], data["height"]
    except (ValueError, TypeError, KeyError):
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool)
               and 0 < v <= MAX_REPORTED for v in (width, height)):
        return None
    return width, height


def fit_size(width, height):
    """The monitor size for a screen of width x height pixels: the same
    aspect ratio, scaled down only if it is bigger than the caps, and even
    in both directions. None for a screen too small to be real."""
    scale = min(1.0, MAX_LONG_SIDE / max(width, height),
                (MAX_AREA / (width * height)) ** 0.5)
    w = int(width * scale) // 2 * 2
    h = int(height * scale) // 2 * 2
    if min(w, h) < MIN_SIDE:
        return None
    return w, h


def load_last_size(path=STATE_PATH):
    """The size fitted last time, or None."""
    try:
        with open(path) as f:
            data = json.load(f)
        return fit_size(int(data["width"]), int(data["height"]))
    except (OSError, ValueError, TypeError, KeyError, ZeroDivisionError):
        return None


def save_last_size(size, path=STATE_PATH):
    """Remember `size` for the next start. Best effort."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"width": size[0], "height": size[1]}, f)
        os.replace(tmp, path)
    except OSError:
        pass


class DeviceFit:
    """Turns SIZE reports into at most one resize request at a time.

    `current()` returns the monitor's size now; `request_restart()` asks the
    capture supervisor to restart the encoder, which first collects the
    wanted size with take_wanted() and resizes the monitor.
    """

    def __init__(self, current, request_restart, log, settle=SETTLE):
        self._current = current
        self._request_restart = request_restart
        self._log = log
        self._settle = settle
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._pending = None        # (size, who, due) waiting to settle
        self._wanted = None         # size the supervisor should apply
        self._stopped = False
        threading.Thread(target=self._run, name="device-fit",
                         daemon=True).start()

    def on_size(self, who, width, height, only_phone, now=False):
        """A phone reported its screen size; `now`: the user turned it."""
        size = fit_size(width, height)
        if size is None:
            self._log(f"{who} reported a {width}x{height} screen; too small "
                      "to fit the monitor to, ignoring it")
            return
        if not only_phone:
            if size != self._current():
                self._log(f"{who}'s screen is {width}x{height}, but more than "
                          "one phone is connected: keeping the monitor at "
                          "{}x{}".format(*self._current()))
            return
        with self._lock:
            due = time.monotonic() + (0 if now else self._settle)
            self._pending = (size, who, due)
        self._wake.set()

    def take_wanted(self):
        """The size to resize to now, or None. Called by the supervisor with
        the encoder stopped."""
        with self._lock:
            wanted, self._wanted = self._wanted, None
        return wanted

    def stop(self):
        self._stopped = True
        self._wake.set()

    def _run(self):
        while not self._stopped:
            with self._lock:
                pending = self._pending
            if pending is None:
                self._wake.wait()
                self._wake.clear()
                continue
            size, who, due = pending
            delay = due - time.monotonic()
            if delay > 0:
                self._wake.wait(delay)      # a newer report moves `due`
                self._wake.clear()
                continue
            with self._lock:
                if self._pending is not pending:
                    continue
                self._pending = None
                if size == self._current():
                    continue
                self._wanted = size
            self._log("fitting the monitor to {}'s screen: {}x{} -> {}x{}"
                      .format(who, *self._current(), *size))
            self._request_restart()


def wants_now(body):
    """True if a SIZE frame body asks for the size at once (a user's turn)."""
    try:
        return json.loads(body).get("now") is True
    except (ValueError, AttributeError):
        return False
