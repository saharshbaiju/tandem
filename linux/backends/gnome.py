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
    # Never raise: once the terminal or the TUI reading our stderr is gone,
    # a print would throw, and in the gst log thread that would stop the
    # draining of gst-launch's stderr until its pipe fills and it stalls.
    try:
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


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
    """True if `element` has property `prop` (per gst-inspect-1.0).

    gst-inspect pads names to a column, but a long name runs straight into
    the colon ("repeat-sequence-header: ..."), so accept either separator.
    """
    return any(line.strip().startswith((prop + " ", prop + ":"))
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


# Rate-control buffer (VBV / CPB) length, in frames. Encoders default to
# 0.6-1.5 s, which lets one IDR grow to many times an average frame and
# serialise the Wi-Fi link for its whole length: a latency spike every GOP.
# Two frames keeps every frame, IDRs included, near the average size.
VBV_FRAMES = 2


def vbv_ms(fps):
    """VBV length in milliseconds (for encoders that size it in time)."""
    return max(1, round(1000 * VBV_FRAMES / max(1, fps)))


def vbv_kbits(kbps, fps):
    """VBV size in kbit (for encoders that size it in bits)."""
    return max(1, kbps * VBV_FRAMES // max(1, fps))


def _with_props(element, *required, **optional):
    """`element req... key=value...`, keeping only optional props it has.

    Optional props are given with "_" for "-" (cpb_size -> cpb-size) and are
    probed with has_prop, so an older plugin without one still parses.
    """
    parts = [element, *required]
    for key, value in optional.items():
        prop = key.replace("_", "-")
        if has_prop(element, prop):
            parts.append(f"{prop}={value}")
    return " ".join(parts)


def videoconvert():
    """CPU colour conversion, threaded where supported (default is 1 thread,
    which is a few ms per 1080p frame of avoidable latency)."""
    return _with_props("videoconvert", n_threads=min(4, os.cpu_count() or 1))


def encoder_chain(enc, kbps, gop, fps=60):
    """GStreamer elements from raw frames to H.264 for `enc`.

    Every chain is single-slice and B-frame free: the phone's hardware decoder
    wants exactly one complete access unit per input buffer. Each one is also
    set up for low latency: fastest preset, no lookahead, and a VBV of
    VBV_FRAMES frames where the element exposes one.
    """
    if enc in ("vah264enc", "vah264lpenc"):
        conv = ("vapostproc ! video/x-raw(memory:VAMemory),format=NV12"
                if gst_inspect("vapostproc")
                else f"{videoconvert()} ! video/x-raw,format=NV12")
        return f"{conv} ! " + _with_props(
            enc, "rate-control=cbr", f"bitrate={kbps}",
            f"key-int-max={gop}", "b-frames=0", "target-usage=7",
            cpb_size=vbv_kbits(kbps, fps))
    if enc == "vaapih264enc":
        return (f"{videoconvert()} ! video/x-raw,format=NV12 ! " + _with_props(
            enc, "rate-control=cbr", f"bitrate={kbps}",
            f"keyframe-period={gop}", "max-bframes=0",
            cpb_length=vbv_ms(fps)))
    if enc == "x264enc":
        # sliced-threads=false keeps one slice per frame; 2 frame threads cost
        # ~1 frame of latency but keep 1080p60 realtime on modest CPUs.
        return (f"{videoconvert()} ! video/x-raw,format=I420 ! " + _with_props(
            enc, "tune=zerolatency", "speed-preset=ultrafast",
            f"bitrate={kbps}", f"key-int-max={gop}", "bframes=0", "threads=2",
            "sliced-threads=false", "byte-stream=true",
            vbv_buf_capacity=vbv_ms(fps)))
    if enc == "openh264enc":
        # openh264 has no VBV-size property; its bitrate mode is already
        # frame-level and skips frames rather than buffering them.
        return (f"{videoconvert()} ! video/x-raw,format=I420 ! openh264enc "
                f"usage-type=screen rate-control=bitrate bitrate={kbps * 1000} "
                f"gop-size={gop} complexity=low")
    raise ValueError(f"unsupported encoder {enc}")


# The single frame of buffering between capture and encoder: when the
# encoder falls behind, drop the oldest raw frame instead of queueing
# (a queue's default 200 buffers / 1 s would hold seconds of lag).
LEAKY_QUEUE = ("queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 "
               "leaky=downstream")


def pipewiresrc(node, extra=""):
    """pipewiresrc for `node`, re-sending the last frame every second where
    the plugin supports it: compositors only send frames on damage, and a
    newly joined phone shouldn't wait on a static desktop for its first one."""
    src = f"pipewiresrc path={node} do-timestamp=true"
    if has_prop("pipewiresrc", "keepalive-time"):
        src += " keepalive-time=1000"
    return f"{src} {extra}".strip()


def gst_launch(desc):
    """argv for gst-launch-1.0. No -e: it makes SIGINT wait for an EOS to
    drain through the pipeline, which can hang a stop on a stalled source;
    our frames are an endless live stream with nothing to finalise."""
    return ["gst-launch-1.0", "-q"] + desc.split()


def build_pipeline(node, width, height, kbps, gop, enc, fd=3, fps=60):
    """gst-launch argv: PipeWire node -> H.264 Annex-B access units on `fd`."""
    desc = (f"{pipewiresrc(node)} ! video/x-raw,width={width},height={height} "
            f"! {LEAKY_QUEUE} "
            f"! {encoder_chain(enc, kbps, gop, fps)} "
            f"! h264parse config-interval=-1 "
            f"! video/x-h264,stream-format=byte-stream,alignment=au "
            f"! fdsink fd={fd} sync=false")
    return gst_launch(desc)


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


# Per-monitor settings GetCurrentState reports (monitor properties) and the
# key ApplyMonitorsConfig takes them back under. A key Mutter doesn't report
# is left out, so its default never overrides what the user chose; an older
# Mutter ignores keys it doesn't know.
MONITOR_PROPS = (("is-underscanning", "underscanning", "b"),
                 ("color-mode", "color-mode", "u"),         # HDR, GNOME 47+
                 ("rgb-range", "rgb-range", "u"))           # GNOME 47+


def _apply_props(mprops):
    """The apply-side properties that keep a monitor exactly as it is."""
    out = {}
    for reported, key, sig in MONITOR_PROPS:
        if reported in mprops:
            value = mprops[reported]
            out[key] = GLib.Variant(sig, bool(value) if sig == "b" else value)
    return out


def _overlaps(a, b):
    """True if rectangles (x, y, w, h) share any area (touching is fine)."""
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def place_beside(anchor, others, ext_size, side):
    """Top-left for a w x h monitor on `side` of `anchor`, overlapping none.

    `anchor` and `others` are (x, y, w, h) rectangles (anchor among others).
    Flush against the anchor, top/left-aligned with it, when that spot is
    free. If another monitor is in the way, go to the outermost edge of the
    whole layout on that side instead and line up with the monitor that
    forms that edge (the one nearest the anchor), so the new monitor still
    shares an edge with something (Mutter rejects detached monitors).
    """
    ax, ay, aw, ah = anchor
    ew, eh = ext_size
    horizontal = side in ("left", "right")
    if side == "left":
        spot = (ax - ew, ay)
    elif side == "up":
        spot = (ax, ay - eh)
    elif side == "down":
        spot = (ax, ay + ah)
    else:
        spot = (ax + aw, ay)
    if not any(_overlaps((*spot, ew, eh), r) for r in others):
        return spot

    # Monitor edge on `side` of each rectangle; the outermost one wins.
    def edge(r):
        x, y, w, h = r
        return {"left": x, "up": y, "down": y + h}.get(side, x + w)

    outer = (min if side in ("left", "up") else max)(edge(r) for r in others)
    # Line up with the edge-forming monitor closest to the anchor, along
    # the edge: same row for left/right, same column for up/down.
    along = (lambda r: abs(r[1] - ay)) if horizontal else \
        (lambda r: abs(r[0] - ax))
    rx, ry, _rw, _rh = min((r for r in others if edge(r) == outer), key=along)
    if side == "left":
        return outer - ew, ry
    if side == "up":
        return rx, outer - eh
    if side == "down":
        return rx, outer
    return outer, ry


class LayoutRefused(RuntimeError):
    """GNOME won't take any layout from us until the user changes something
    in Settings → Displays: retrying can't help, so don't."""


def _scale_ok(scale, supported):
    return not supported or any(abs(scale - s) < 1e-3 for s in supported)


def place_monitor(bus, connector, side, scale):
    """Put `connector` on `side` of the primary monitor and set its scale.

    Mutter applies a whole layout at once, so every other monitor is re-sent
    exactly as GetCurrentState reports it: same mode, scale, transform,
    primary flag, position (relative to each other) and per-monitor settings
    such as underscan and HDR colour mode. The layout is then shifted so its
    top-left is 0,0, which Mutter requires. Nothing is sent if the virtual
    monitor is already where it should be.

    Returns (changed, scale, logical): whether a change was sent, the scale
    the monitor has (the supported one nearest to `scale`), and whether the
    layout is in logical mode (positions in logical pixels: size / scale).
    Raises LayoutRefused when another monitor's own scale would make Mutter
    reject any layout.
    """
    serial, monitors, logicals, props = display_state(bus)
    physical = props.get("layout-mode", 1) == 2

    current = {}            # connector -> (mode_id, w, h, scales, apply props)
    names = {}              # connector -> what Settings calls it
    for spec, modes, mprops in monitors:
        names[spec[0]] = mprops.get("display-name") or spec[0]
        for mid, w, h, _rate, _pref, scales, modeprops in modes:
            if modeprops.get("is-current"):
                current[spec[0]] = (mid, w, h, scales, _apply_props(mprops))

    lms = []
    for x, y, sc, transform, primary, specs, _lp in logicals:
        lms.append({"x": x, "y": y, "scale": sc, "transform": transform,
                    "primary": primary, "conns": [s[0] for s in specs]})
    ext = next((lm for lm in lms if connector in lm["conns"]), None)
    if ext is None or connector not in current:
        raise RuntimeError(f"virtual monitor {connector} not in layout yet")
    if any(c not in current for lm in lms for c in lm["conns"]):
        raise RuntimeError("a monitor has no current mode (layout changing)")

    def layout():
        """The layout as plain values, normalised to 0,0, for comparison."""
        minx = min(lm["x"] for lm in lms)
        miny = min(lm["y"] for lm in lms)
        return [(lm["x"] - minx, lm["y"] - miny, float(lm["scale"]),
                 lm["transform"], bool(lm["primary"]), tuple(lm["conns"]))
                for lm in lms]

    before = layout()

    others = [lm for lm in lms if lm is not ext]
    if scale:
        supported = current[connector][3] or [1.0]
        if props.get("global-scale-required") and others:
            # This Mutter can only use one scale for all monitors.
            ext["scale"] = others[0]["scale"]
        else:
            ext["scale"] = min(supported, key=lambda s: abs(s - scale))

    def rect(lm):
        _mid, w, h, _sc, _p = current[lm["conns"][0]]
        if lm["transform"] % 2:                   # 90 / 270 degrees
            w, h = h, w
        if not physical:
            w, h = int(round(w / lm["scale"])), int(round(h / lm["scale"]))
        return lm["x"], lm["y"], w, h

    if others:
        # The primary is the anchor (deterministic); if there's none, the
        # top-left-most monitor.
        anchor = next((lm for lm in others if lm["primary"]),
                      min(others, key=lambda lm: (lm["x"], lm["y"])))
        ext["x"], ext["y"] = place_beside(rect(anchor),
                                          [rect(lm) for lm in others],
                                          rect(ext)[2:], side)
        if ext["primary"]:
            # Never leave the primary on the phone, and never leave the
            # layout without one (Mutter rejects a config with no primary).
            ext["primary"] = False
            anchor["primary"] = True

    after = layout()
    if after == before:
        return False, ext["scale"], not physical

    # Every monitor is re-sent at its current scale, and Mutter checks them
    # all. It can be running a scale it no longer accepts (a 150% set by an
    # older GNOME on a 3000x2000 panel, where 150% gives fractional pixels):
    # then *every* layout change fails, ours and Settings' alike, with an
    # error that doesn't say what to do. Say it.
    for lm in others:
        conn = lm["conns"][0]
        _mid, w, h, supported, _p = current[conn]
        if not _scale_ok(lm["scale"], supported):
            raise LayoutRefused(
                f"GNOME refuses every display change while "
                f"\"{names[conn]}\" ({w}x{h}) is at {lm['scale'] * 100:.0f}%, "
                "a scale it no longer allows at that resolution. Pick "
                "another scale for it in Settings → Displays (e.g. "
                + " or ".join(f"{sc * 100:.0f}%" for sc in sorted(
                    supported, key=lambda sc: abs(sc - lm["scale"]))[:2])
                + "), then try again")

    config = [(x, y, sc, transform, primary,
               [(c, current[c][0], current[c][4]) for c in conns])
              for x, y, sc, transform, primary, conns in after]
    global_props = {}
    if props.get("supports-changing-layout-mode") and "layout-mode" in props:
        # Without it Mutter falls back to its *default* layout mode, which
        # may not be the one our coordinates were computed in.
        global_props["layout-mode"] = GLib.Variant("u", props["layout-mode"])
    _call(bus, DC_BUS, DC_PATH, DC_IFACE, "ApplyMonitorsConfig",
          GLib.Variant("(uua(iiduba(ssa{sv}))a{sv})",
                       (serial, APPLY_TEMPORARY, config, global_props)))
    return True, ext["scale"], not physical


# --------------------------------------------------------------------------- #
# Touch input (Mutter RemoteDesktop)
# --------------------------------------------------------------------------- #

class _DBusWorker:
    """A thread with its own GLib main loop, for asynchronous D-Bus calls.

    An async Gio call delivers its reply on the main context it was made
    from, and nothing iterates the default context during a session. So the
    calls are made from here: run(fn) queues fn onto this loop (in order) and
    returns at once, and the replies are handled here too.
    """

    def __init__(self, name):
        self._ctx = GLib.MainContext.new()
        self._loop = GLib.MainLoop.new(self._ctx, False)
        self._thread = threading.Thread(target=self._main, name=name,
                                        daemon=True)
        self._thread.start()

    def _main(self):
        self._ctx.push_thread_default()   # async replies come back here
        try:
            self._loop.run()
        finally:
            self._ctx.pop_thread_default()

    def run(self, fn):
        """Run fn() on the worker thread, after everything queued before."""
        def once():
            fn()
            return False                   # one-shot source
        source = GLib.idle_source_new()
        source.set_callback(once)
        source.attach(self._ctx)           # thread-safe; wakes the loop

    def drain(self, timeout):
        """Wait (up to `timeout` s) until everything queued so far has run."""
        done = threading.Event()
        self.run(done.set)
        return done.wait(timeout)

    def stop(self):
        self.run(self._loop.quit)


class MutterTouchscreen:
    """Touches injected through a RemoteDesktop session onto our stream.

    Mutter takes coordinates in the monitor's *logical* pixels: its width x
    height divided by its scale (in the usual logical layout mode; in the
    physical one the scale doesn't apply). At 267% a 2160-px-wide monitor is
    810 logical px wide, so physical positions would land 2.67x too far or
    off the monitor. The phone's fractions are scaled to that logical area,
    kept current by resize() (new monitor size) and rescale() (new scale).

    Calls never block the caller: gnome-shell answers on its main loop,
    which is busiest exactly while a drag is being composited, and a
    synchronous round-trip per event would stall the thread that also sends
    the video. Each call is sent asynchronously from a _DBusWorker. Down and
    up are always sent, in order; motion is coalesced per finger while
    MAX_IN_FLIGHT calls are unanswered, so a slow gnome-shell sees fewer,
    newer positions instead of a growing backlog.
    """

    slot_count = 10
    MAX_IN_FLIGHT = 4

    def __init__(self, bus, rd_session, stream_path, width, height):
        self.description = "Mutter RemoteDesktop touch on the virtual monitor"
        self._bus = bus
        self._session = rd_session
        self._stream = stream_path
        self._size = (width, height)      # physical pixels
        self._scale = 1.0                 # pixels per logical pixel
        self._area = (width, height)      # logical size touches are scaled to
        self._active_slots = set()
        self._warned = False
        self._worker = _DBusWorker("tandem-mutter-touch")
        # Worker-thread state only (no lock needed):
        self._in_flight = 0
        self._pending_motion = {}         # slot -> (px, py), newest only

    def down(self, slot, x, y):
        self._active_slots.add(slot)
        px, py = self._to_pixels(x, y)
        self._worker.run(lambda: self._send_now(
            slot, "NotifyTouchDown", "(sudd)", (self._stream, slot, px, py)))

    def move(self, slot, x, y):
        if slot in self._active_slots:
            px, py = self._to_pixels(x, y)
            self._worker.run(lambda: self._send_motion(slot, px, py))

    def up(self, slot):
        if slot in self._active_slots:
            self._active_slots.discard(slot)
            self._worker.run(lambda: self._send_now(
                slot, "NotifyTouchUp", "(u)", (slot,)))

    def release_all(self):
        for slot in sorted(self._active_slots):
            self.up(slot)

    def close(self):
        """Lift every finger and let the calls go out before the session is
        stopped in teardown(), then end the worker thread."""
        self.release_all()
        self._worker.drain(1.0)
        self._worker.stop()

    def resize(self, width, height):
        """The monitor was resized (in physical pixels)."""
        self._size = (width, height)
        self._update_area()

    def rescale(self, scale):
        """The monitor's scale changed; 1.0 in the physical layout mode."""
        self._scale = scale
        self._update_area()

    def _update_area(self):
        width, height = self._size
        self._area = (width / self._scale, height / self._scale)

    def _to_pixels(self, x, y):
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        width, height = self._area          # one read: resize() may race
        return x * (width - 1), y * (height - 1)

    # -- worker thread ------------------------------------------------------ #

    def _send_motion(self, slot, px, py):
        if self._in_flight >= self.MAX_IN_FLIGHT:
            self._pending_motion[slot] = (px, py)  # replaced by newer ones
            return
        self._pending_motion.pop(slot, None)
        self._call("NotifyTouchMotion", "(sudd)", (self._stream, slot, px, py))

    def _send_now(self, slot, method, signature, values):
        """Down / up: never coalesced or dropped. A finger's held-back
        motion goes first so its last position isn't lost."""
        held = self._pending_motion.pop(slot, None)
        if held:
            self._call("NotifyTouchMotion", "(sudd)",
                       (self._stream, slot, *held))
        self._call(method, signature, values)

    def _call(self, method, signature, values):
        self._bus.call(RD_BUS, self._session, RD_SESSION_IFACE, method,
                       GLib.Variant(signature, values), None,
                       Gio.DBusCallFlags.NO_AUTO_START, 1000, None,
                       self._on_reply, None)
        self._in_flight += 1               # only once a reply is owed

    def _on_reply(self, bus, result, *_user_data):
        self._in_flight -= 1
        try:
            bus.call_finish(result)
        except Exception as e:
            if not self._warned:          # once, not at 120 events/s
                self._warned = True
                log(f"warning: touch event rejected by Mutter ({e})")
        # A reply frees a slot: send the newest held-back motion, if any.
        if self._pending_motion and self._in_flight < self.MAX_IN_FLIGHT:
            slot, (px, py) = next(iter(self._pending_motion.items()))
            self._send_motion(slot, px, py)


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
        # apply_live (control thread) and the placement watcher can run at
        # once; two ApplyMonitorsConfig calls on one serial would fail.
        self._place_lock = threading.Lock()
        self._scale = None              # the scale last applied and logged
        self._touch = None              # MutterTouchscreen, once created
        self._touch_scale = None        # the scale touches must divide by

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
        self._touch = MutterTouchscreen(self.bus, self.remote_desktop,
                                        self.stream, self.a.width,
                                        self.a.height)
        if self._touch_scale:
            self._touch.rescale(self._touch_scale)
        return self._touch

    def start_capture(self):
        """Start the encoder on our PipeWire node; returns its H.264 stream.

        Safe to call again after stop_capture() (or after gst-launch exited
        on its own, e.g. a renegotiation) within one session: the virtual
        monitor and touch session belong to setup()/teardown(), not to this.
        """
        a = self.a
        if a.codec != "h264":
            log("note: GNOME backend encodes h264 only; ignoring codec "
                f"'{a.codec}'")
        enc = pick_encoder(getattr(a, "encoder", None))
        if not enc:
            raise RuntimeError("no usable GStreamer H.264 encoder found")
        self.stop_capture()                 # never two encoders on one node
        rfd, wfd = os.pipe()
        cmd = build_pipeline(self.node, a.width, a.height,
                             bitrate_kbps(a.bitrate), a.gop, enc, fd=wfd,
                             fps=getattr(a, "fps", 60) or 60)
        log(f"encoder {enc}; capture cmd: " + " ".join(cmd))
        try:
            # Own session: a Ctrl-C or hangup on the launching terminal
            # must not kill the encoder behind the daemon's back. It still
            # dies with the daemon: its next write to the pipe gets EPIPE,
            # and the PipeWire node goes when our D-Bus connection closes.
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL,
                                    stderr=subprocess.PIPE, pass_fds=(wfd,),
                                    start_new_session=True)
        except BaseException:
            os.close(rfd)
            raise
        finally:
            os.close(wfd)          # our copy; EOF arrives when gst exits
        self.proc = proc
        # Both threads get *this* process: after a restart, self.proc is the
        # next one and the old threads must finish with the old one.
        threading.Thread(target=self._gst_log, args=(proc,),
                         daemon=True).start()
        threading.Thread(target=self._place_when_ready, args=(proc,),
                         daemon=True).start()
        return os.fdopen(rfd, "rb", buffering=0)

    @staticmethod
    def _gst_log(proc):
        for line in proc.stderr:
            line = line.decode("utf-8", "replace").rstrip()
            if line:
                log("gst: " + line)

    def _place_when_ready(self, proc):
        """Wait for Mutter to add the monitor (after caps negotiate), place it.

        Keeps watching for a few seconds after that: on a capture restart
        Mutter may drop the old virtual monitor and add a new one (possibly
        under a new connector name, at its default spot) once the new
        pipeline negotiates. place_monitor sends nothing when the monitor is
        already in place, so re-checking is free of flicker.
        """
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        deadline = time.monotonic() + 15
        settle_until = None
        error = None
        while time.monotonic() < deadline and proc.poll() is None:
            if settle_until and time.monotonic() > settle_until:
                return
            new = connectors(bus) - self._before
            if new:
                connector = (self.connector if self.connector in new
                             else sorted(new)[0])
                if connector != self.connector:
                    self.connector = connector
                    log(f"virtual monitor appeared as {connector}")
                # Errors are expected while Mutter is still adding it to
                # the layout; only the last one is worth reporting.
                error = self._place(self.a.side, self.a.scale, bus)
                if isinstance(error, LayoutRefused):
                    log(f"warning: {error}")
                    return              # retrying can't fix it
                if error is None:
                    settle_until = settle_until or time.monotonic() + 5
            time.sleep(0.25)
        if settle_until is None and proc.poll() is None:
            log("warning: could not place virtual monitor "
                f"({error or 'it never appeared in DisplayConfig'})")

    def resize(self):
        """Nothing to do here: the encoder's caps set the monitor's size,
        and Mutter resizes the virtual monitor in place (same connector,
        windows stay) when capture restarts at the new size."""

    def apply_live(self, side, scale, bus=None):
        """Move / rescale the monitor. True if the encoder must restart: a
        running pipewiresrc gets no more frames after Mutter changes the
        monitor's scale (seen on GNOME 51), while a new one on the same
        PipeWire node works."""
        if not self.connector:
            return False
        before = self._scale
        error = self._place(side, scale, bus)
        if error:
            log(f"warning: could not place virtual monitor ({error})")
            return False
        return self._scale != before

    def _place(self, side, scale, bus=None):
        """place_monitor under the lock; returns None or the error."""
        try:
            with self._place_lock:
                changed, applied, logical = place_monitor(
                    bus or Gio.bus_get_sync(Gio.BusType.SESSION, None),
                    self.connector, side, scale)
        except Exception as e:
            return e
        if changed:
            log(f"placed {self.connector} side={side}")
        if applied != self._scale:
            self._scale = applied
            # The TUI shows this: it can differ from the asked-for scale.
            log(f"monitor scale: {applied:g} (asked {scale:g})")
        self._touch_scale = applied if logical else 1.0
        if self._touch:
            self._touch.rescale(self._touch_scale)
        return None

    def stop_capture(self):
        """Stop the encoder only (the virtual monitor stays). Idempotent."""
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()

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
