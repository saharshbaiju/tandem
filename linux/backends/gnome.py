"""
gnome.py - GNOME (Mutter, Wayland) backend: Ubuntu, Fedora, any GNOME 40+.

Mutter has no "create a headless output" command like Hyprland, but its private
ScreenCast D-Bus API can create a *virtual monitor* (the same mechanism GNOME
Remote Desktop uses for "extend" sessions):

    org.gnome.Mutter.ScreenCast.CreateSession()
      -> Session.RecordVirtual({cursor-mode: embedded, is-platform: true})
      -> Session.Start()  -> Stream emits PipeWireStreamAdded(node_id)

The monitor only comes into existence once a PipeWire consumer negotiates a
format, and it takes the *size the consumer asks for*. So the width/height in
our GStreamer caps literally decide the virtual monitor's resolution:

    gst-launch-1.0 pipewiresrc path=<node> ! video/x-raw,width=W,height=H
        ! <H.264 encoder> ! h264parse ! byte-stream AU ! fdsink fd=3
        -> raw Annex-B on a pipe -> tandemd's parser (same as Hyprland)

The virtual monitor lives exactly as long as our D-Bus connection: when the
daemon exits, Mutter removes it and moves its windows back automatically.

Side and scale are applied through org.gnome.Mutter.DisplayConfig
(ApplyMonitorsConfig, method "temporary" so nothing is written to
monitors.xml and no "keep these settings?" dialog appears).

Touch input uses Mutter's RemoteDesktop API (what GNOME Remote Desktop uses).
A RemoteDesktop session is created first and the ScreenCast session is linked
to it (`remote-desktop-session-id`); the linked pair is then started through
the RemoteDesktop session. Touches are sent with NotifyTouchDown/Motion/Up
against our virtual-monitor stream, so they land on the phone's monitor and the
mouse pointer doesn't move. No root, no uinput. If the RemoteDesktop session
can't be created, the backend falls back to a plain ScreenCast session (video
works, touch is reported as unavailable).
"""

import os
import shutil
import subprocess
import sys
import threading
import time

try:
    from gi.repository import Gio, GLib
except Exception:              # reported via missing()
    Gio = GLib = None

SC_BUS = "org.gnome.Mutter.ScreenCast"
SC_PATH = "/org/gnome/Mutter/ScreenCast"
SC_IFACE = "org.gnome.Mutter.ScreenCast"
RD_BUS = "org.gnome.Mutter.RemoteDesktop"
RD_PATH = "/org/gnome/Mutter/RemoteDesktop"
RD_IFACE = "org.gnome.Mutter.RemoteDesktop"
RD_SESSION_IFACE = RD_IFACE + ".Session"
DC_BUS = "org.gnome.Mutter.DisplayConfig"
DC_PATH = "/org/gnome/Mutter/DisplayConfig"
DC_IFACE = "org.gnome.Mutter.DisplayConfig"

CURSOR_EMBEDDED = 1            # draw the pointer into the frames
APPLY_TEMPORARY = 1            # DisplayConfig method: don't persist, no dialog

# Hardware encoders first; the VA plugins only register an element when the
# driver can really encode H.264, so "element exists" is a usable probe.
ENCODERS = ("vah264enc", "vah264lpenc", "vaapih264enc", "x264enc", "openh264enc")


def log(*a):
    print("[tandemd]", *a, file=sys.stderr, flush=True)


def is_gnome_wayland():
    desk = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return "GNOME" in desk and bool(os.environ.get("WAYLAND_DISPLAY"))


# --------------------------------------------------------------------------- #
# GStreamer probing
# --------------------------------------------------------------------------- #

_inspect_cache = {}


def gst_inspect(element):
    """`gst-inspect-1.0 element` output, or "" if the element is missing."""
    if element not in _inspect_cache:
        try:
            r = subprocess.run(["gst-inspect-1.0", element], capture_output=True,
                               text=True, timeout=10)
            _inspect_cache[element] = r.stdout if r.returncode == 0 else ""
        except Exception:
            _inspect_cache[element] = ""
    return _inspect_cache[element]


def has_prop(element, prop):
    return any(line.strip().startswith(prop + " ")
               for line in gst_inspect(element).splitlines())


def pick_encoder(preferred=None):
    if preferred and preferred != "auto":
        return preferred if gst_inspect(preferred) else None
    return next((e for e in ENCODERS if gst_inspect(e)), None)


def bitrate_kbps(bitrate):
    """'12M' / '800K' / '12000000' -> kbit/s."""
    s = str(bitrate).strip().upper()
    if s.endswith("M"):
        return int(float(s[:-1]) * 1000)
    if s.endswith("K"):
        return int(float(s[:-1]))
    return max(1, int(float(s)) // 1000)


def encoder_chain(enc, kbps, gop):
    """GStreamer elements from raw frames to H.264 for `enc`.

    Every chain is single-slice and B-frame free: the phone's hardware decoder
    wants exactly one complete access unit per input buffer.
    """
    if enc in ("vah264enc", "vah264lpenc"):
        conv = ("vapostproc ! video/x-raw(memory:VAMemory),format=NV12"
                if gst_inspect("vapostproc")
                else "videoconvert ! video/x-raw,format=NV12")
        return (f"{conv} ! {enc} rate-control=cbr bitrate={kbps} "
                f"key-int-max={gop} b-frames=0 target-usage=7")
    if enc == "vaapih264enc":
        return (f"videoconvert ! video/x-raw,format=NV12 ! vaapih264enc "
                f"rate-control=cbr bitrate={kbps} keyframe-period={gop} "
                f"max-bframes=0")
    if enc == "x264enc":
        # sliced-threads=false keeps one slice per frame; 2 frame threads cost
        # ~1 frame of latency but keep 1080p60 realtime on modest CPUs.
        return (f"videoconvert ! video/x-raw,format=I420 ! x264enc "
                f"tune=zerolatency speed-preset=ultrafast bitrate={kbps} "
                f"key-int-max={gop} bframes=0 threads=2 sliced-threads=false "
                f"byte-stream=true")
    if enc == "openh264enc":
        return (f"videoconvert ! video/x-raw,format=I420 ! openh264enc "
                f"usage-type=screen rate-control=bitrate bitrate={kbps * 1000} "
                f"gop-size={gop} complexity=low")
    raise ValueError(f"unsupported encoder {enc}")


def build_pipeline(node, width, height, kbps, gop, enc, fd=3):
    """gst-launch argv: PipeWire node -> H.264 Annex-B access units on `fd`."""
    src = f"pipewiresrc path={node} do-timestamp=true"
    if has_prop("pipewiresrc", "keepalive-time"):
        # Mutter only sends frames on damage; re-send the last one every second
        # so a newly joined phone isn't stuck waiting on a static desktop.
        src += " keepalive-time=1000"
    desc = (f"{src} ! video/x-raw,width={width},height={height} "
            f"! queue max-size-buffers=1 leaky=downstream "
            f"! {encoder_chain(enc, kbps, gop)} "
            f"! h264parse config-interval=-1 "
            f"! video/x-h264,stream-format=byte-stream,alignment=au "
            f"! fdsink fd={fd} sync=false")
    return ["gst-launch-1.0", "-q", "-e"] + desc.split()


# --------------------------------------------------------------------------- #
# Mutter D-Bus helpers
# --------------------------------------------------------------------------- #

def _call(bus, name, path, iface, method, args=None, timeout=5000):
    return bus.call_sync(name, path, iface, method, args, None,
                         Gio.DBusCallFlags.NONE, timeout, None).unpack()


def display_state(bus):
    """(serial, monitors, logical_monitors, properties) from DisplayConfig."""
    return _call(bus, DC_BUS, DC_PATH, DC_IFACE, "GetCurrentState")


def connectors(bus):
    try:
        return {spec[0] for spec, _modes, _props in display_state(bus)[1]}
    except Exception:
        return set()


def place_monitor(bus, connector, side, scale):
    """Move `connector` flush against the primary on `side` and set its scale.

    Rebuilds the whole layout (Mutter applies configs atomically), keeps every
    other monitor where it is, then shifts everything so the top-left is 0,0 —
    Mutter rejects layouts with offset or non-adjacent monitors.
    """
    serial, monitors, logicals, props = display_state(bus)
    physical = props.get("layout-mode", 1) == 2

    current = {}                                  # connector -> (mode_id, w, h, scales)
    for spec, modes, _mprops in monitors:
        for mid, w, h, _rate, _pref, scales, mprops in modes:
            if mprops.get("is-current"):
                current[spec[0]] = (mid, w, h, scales)

    lms = []
    for x, y, sc, transform, primary, specs, _lp in logicals:
        lms.append({"x": x, "y": y, "scale": sc, "transform": transform,
                    "primary": primary, "conns": [s[0] for s in specs]})
    ext = next((lm for lm in lms if connector in lm["conns"]), None)
    if ext is None or connector not in current:
        raise RuntimeError(f"virtual monitor {connector} not in layout yet")

    if scale:
        supported = current[connector][3] or [1.0]
        ext["scale"] = min(supported, key=lambda s: abs(s - scale))

    def size(lm):
        _mid, w, h, _sc = current[lm["conns"][0]]
        if lm["transform"] % 2:                   # 90 / 270 degrees
            w, h = h, w
        if physical:
            return w, h
        return int(round(w / lm["scale"])), int(round(h / lm["scale"]))

    others = [lm for lm in lms if lm is not ext]
    if others:
        pr = next((lm for lm in others if lm["primary"]), others[0])
        pw, ph = size(pr)
        ew, eh = size(ext)
        ext["x"], ext["y"] = {
            "left": (pr["x"] - ew, pr["y"]),
            "up": (pr["x"], pr["y"] - eh),
            "down": (pr["x"], pr["y"] + ph),
        }.get(side, (pr["x"] + pw, pr["y"]))
        ext["primary"] = False

    minx = min(lm["x"] for lm in lms)
    miny = min(lm["y"] for lm in lms)
    config = [(lm["x"] - minx, lm["y"] - miny, float(lm["scale"]),
               lm["transform"], lm["primary"],
               [(c, current[c][0], {}) for c in lm["conns"]])
              for lm in lms]
    _call(bus, DC_BUS, DC_PATH, DC_IFACE, "ApplyMonitorsConfig",
          GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})",
                       (serial, APPLY_TEMPORARY, config, {})))


# --------------------------------------------------------------------------- #
# Touch input (Mutter RemoteDesktop)
# --------------------------------------------------------------------------- #

class MutterTouchscreen:
    """Touches injected through a RemoteDesktop session onto our stream.

    Mutter takes coordinates in the stream's own pixel space (the virtual
    monitor's width x height), so fractions from the phone are scaled by the
    monitor size we asked for.
    """

    slot_count = 10

    def __init__(self, bus, rd_session, stream_path, width, height):
        self.description = "Mutter RemoteDesktop touch on the virtual monitor"
        self._bus = bus
        self._session = rd_session
        self._stream = stream_path
        self._width = width
        self._height = height
        self._active_slots = set()
        self._warned = False

    def down(self, slot, x, y):
        self._active_slots.add(slot)
        self._notify("NotifyTouchDown", "(sudd)",
                     (self._stream, slot, *self._to_pixels(x, y)))

    def move(self, slot, x, y):
        if slot in self._active_slots:
            self._notify("NotifyTouchMotion", "(sudd)",
                         (self._stream, slot, *self._to_pixels(x, y)))

    def up(self, slot):
        if slot in self._active_slots:
            self._active_slots.discard(slot)
            self._notify("NotifyTouchUp", "(u)", (slot,))

    def release_all(self):
        for slot in sorted(self._active_slots):
            self.up(slot)

    def close(self):
        self.release_all()      # the session itself is stopped in teardown()

    def _to_pixels(self, x, y):
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        return x * (self._width - 1), y * (self._height - 1)

    def _notify(self, method, signature, values):
        try:
            _call(self._bus, RD_BUS, self._session, RD_SESSION_IFACE, method,
                  GLib.Variant(signature, values), timeout=1000)
        except Exception as e:
            if not self._warned:          # once, not at 120 events/s
                self._warned = True
                log(f"warning: touch event rejected by Mutter ({e})")


# --------------------------------------------------------------------------- #
# backend
# --------------------------------------------------------------------------- #

class GnomeBackend:
    name = "gnome"

    def __init__(self, args):
        self.a = args
        self.bus = None
        self.session = None             # ScreenCast session path
        self.remote_desktop = None      # linked RemoteDesktop session, or None
        self.touch_problem = ""         # why remote_desktop is None
        self.stream = None              # ScreenCast stream path (touch target)
        self.node = None
        self.proc = None
        self.connector = None
        self._before = set()

    @staticmethod
    def missing():
        miss = [t for t in ("gst-launch-1.0", "gst-inspect-1.0")
                if not shutil.which(t)]
        if Gio is None:
            miss.append("python-gobject (gi)")
        if shutil.which("gst-inspect-1.0"):
            for el in ("pipewiresrc", "h264parse", "videoconvert"):
                if not gst_inspect(el):
                    miss.append(f"gstreamer:{el}")
            if not pick_encoder():
                miss.append("gstreamer:h264-encoder")
        return miss

    def setup(self):
        """Open a Mutter screencast session with a virtual-monitor stream.

        With touch enabled, the screencast session is linked to a new
        RemoteDesktop session, and the pair is started through the latter.
        """
        self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        self._before = connectors(self.bus)
        if getattr(self.a, "touch", False):
            self._open_remote_desktop()

        session_options = {}
        if self.remote_desktop:
            session_id = _call(self.bus, RD_BUS, self.remote_desktop,
                               "org.freedesktop.DBus.Properties", "Get",
                               GLib.Variant("(ss)", (RD_SESSION_IFACE,
                                                     "SessionId")))[0]
            session_options["remote-desktop-session-id"] = GLib.Variant(
                "s", session_id)
        (self.session,) = _call(self.bus, SC_BUS, SC_PATH, SC_IFACE,
                                "CreateSession",
                                GLib.Variant("(a{sv})", (session_options,)))
        props = {"cursor-mode": GLib.Variant("u", CURSOR_EMBEDDED),
                 "is-platform": GLib.Variant("b", True)}
        (self.stream,) = _call(self.bus, SC_BUS, self.session,
                               SC_IFACE + ".Session", "RecordVirtual",
                               GLib.Variant("(a{sv})", (props,)))

        got = {}

        def on_added(_c, _s, _p, _i, _sig, params):
            got["node"] = params.unpack()[0]

        sub = self.bus.signal_subscribe(SC_BUS, SC_IFACE + ".Stream",
                                        "PipeWireStreamAdded", self.stream,
                                        None, Gio.DBusSignalFlags.NONE,
                                        on_added)
        if self.remote_desktop:
            # A linked screencast session is started through its
            # RemoteDesktop session (as gnome-remote-desktop does).
            _call(self.bus, RD_BUS, self.remote_desktop, RD_SESSION_IFACE,
                  "Start")
        else:
            _call(self.bus, SC_BUS, self.session, SC_IFACE + ".Session",
                  "Start")
        ctx = GLib.MainContext.default()
        deadline = time.monotonic() + 10
        while "node" not in got and time.monotonic() < deadline:
            ctx.iteration(False)
            time.sleep(0.01)
        self.bus.signal_unsubscribe(sub)
        if "node" not in got:
            raise RuntimeError("Mutter never announced the PipeWire stream")
        self.node = got["node"]
        log(f"mutter virtual monitor stream on pipewire node {self.node}")

    def _open_remote_desktop(self):
        """Create the RemoteDesktop session touch input needs (best effort)."""
        try:
            (self.remote_desktop,) = _call(self.bus, RD_BUS, RD_PATH, RD_IFACE,
                                           "CreateSession")
        except Exception as e:
            self.remote_desktop = None
            self.touch_problem = ("GNOME refused a remote-desktop session "
                                  f"({e})")
            log(f"touch input: {self.touch_problem}; streaming without it")

    def create_touch_surface(self):
        if not self.remote_desktop:
            raise RuntimeError(self.touch_problem
                               or "touch input is turned off on the computer")
        return MutterTouchscreen(self.bus, self.remote_desktop, self.stream,
                                 self.a.width, self.a.height)

    def start_capture(self):
        a = self.a
        if a.codec != "h264":
            log("note: GNOME backend encodes h264 only; ignoring codec "
                f"'{a.codec}'")
        enc = pick_encoder(getattr(a, "encoder", None))
        if not enc:
            raise RuntimeError("no usable GStreamer H.264 encoder found")
        rfd, wfd = os.pipe()
        cmd = build_pipeline(self.node, a.width, a.height,
                             bitrate_kbps(a.bitrate), a.gop, enc, fd=wfd)
        log(f"encoder {enc}; capture cmd: " + " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, pass_fds=(wfd,))
        os.close(wfd)              # our copy; EOF arrives when gst exits
        threading.Thread(target=self._gst_log, daemon=True).start()
        threading.Thread(target=self._place_when_ready, daemon=True).start()
        return os.fdopen(rfd, "rb", buffering=0)

    def _gst_log(self):
        for line in self.proc.stderr:
            line = line.decode("utf-8", "replace").rstrip()
            if line:
                log("gst: " + line)

    def _place_when_ready(self):
        """Wait for Mutter to add the monitor (after caps negotiate), place it."""
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline and self.proc.poll() is None:
            new = connectors(bus) - self._before
            if new:
                self.connector = sorted(new)[0]
                log(f"virtual monitor appeared as {self.connector}")
                self.apply_live(self.a.side, self.a.scale, bus)
                return
            time.sleep(0.2)
        log("warning: virtual monitor never appeared in DisplayConfig")

    def apply_live(self, side, scale, bus=None):
        if not self.connector:
            return False
        try:
            place_monitor(bus or Gio.bus_get_sync(Gio.BusType.SESSION, None),
                          self.connector, side, scale)
            log(f"placed {self.connector} side={side} scale={scale}")
            return True
        except Exception as e:
            log(f"warning: could not place virtual monitor ({e})")
            return False

    def stop_capture(self):
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()

    def teardown(self):
        """Stop the session: Mutter removes the monitor and rehomes windows.

        A linked pair is stopped through the RemoteDesktop session.
        """
        if self.bus and self.session:
            try:
                if self.remote_desktop:
                    _call(self.bus, RD_BUS, self.remote_desktop,
                          RD_SESSION_IFACE, "Stop")
                else:
                    _call(self.bus, SC_BUS, self.session,
                          SC_IFACE + ".Session", "Stop")
            except Exception:
                pass
            self.session = self.remote_desktop = None
            log("stopped mutter screencast session (virtual monitor removed)")
