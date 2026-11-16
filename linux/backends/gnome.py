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

The pipeline itself (encoder choice, gst-launch, its log) is shared with the
other PipeWire backends and lives in gstreamer.py.

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
import sys
import threading
import time

try:
    from gi.repository import Gio, GLib
except Exception:              # reported via missing()
    Gio = GLib = None

# The GStreamer and layout helpers started out in this module and moved out
# so the other PipeWire backends share them. They are re-exported by name:
# xdph.py, hyprland.py, core.py and testsrc.py use them as gnome.<name>.
from .gstreamer import (  # noqa: F401  (re-exported)
    ENCODERS,
    LEAKY_QUEUE,
    VBV_FRAMES,
    _inspect_cache,
    _with_props,
    bitrate_kbps,
    build_pipeline,
    encoder_chain,
    gst_inspect,
    gst_launch,
    has_prop,
    pick_encoder,
    pipewiresrc,
    spawn_pipeline,
    stop_pipeline,
    vbv_kbits,
    vbv_ms,
    videoconvert,
)
from .gstreamer import choose_encoder, missing_pipeline_tools, start_encoder
from .layout import _overlaps, place_beside  # noqa: F401  (re-exported)

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


def log(*a):
    # Never raise: once the terminal or the TUI reading our stderr is gone,
    # a print would throw, and in a helper thread (placement, touch replies)
    # that would end the thread with the work half done.
    try:
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def is_gnome_wayland():
    desk = os.environ.get("XDG_CURRENT_DESKTOP", "").upper()
    return "GNOME" in desk and bool(os.environ.get("WAYLAND_DISPLAY"))


# --------------------------------------------------------------------------- #
# Module hooks (see backends/__init__.py): used without a backend instance
# --------------------------------------------------------------------------- #

def session_confidence():
    """2 in a GNOME Wayland session; 1 when XDG_CURRENT_DESKTOP names GNOME
    without Wayland (X11: setup fails, session_problem() explains); else 0."""
    if is_gnome_wayland():
        return 2
    if "gnome" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return 1
    return 0


def session_problem():
    """Why this session can't host a virtual monitor (lines for the TUI)."""
    if is_gnome_wayland():
        return None
    return ["GNOME is running on X11 — virtual monitors need Wayland.",
            "Log out, click the gear on the login screen and pick "
            "'Ubuntu' / 'GNOME' (not 'on Xorg')."]


def describe_primary_monitor():
    """'<connector>  at x,y  scale s' for the primary monitor, or None."""
    if Gio is None:
        return None
    try:
        bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
        _serial, _monitors, logicals, _props = display_state(bus)
        for x, y, scale, _transform, primary, specs, _lprops in logicals:
            if primary:
                return f"{specs[0][0]}  at {x},{y}  scale {scale:g}"
    except Exception:
        pass
    return None


def remove_leftover_output(output_name):
    """Nothing to do: Mutter removes the virtual monitor (and moves its
    windows back) as soon as the daemon's D-Bus connection closes, so none
    can outlive the daemon."""
    return None


def touch_setup_problem():
    """None: touches go through Mutter's RemoteDesktop API, which needs no
    permissions or one-time setup."""
    return None


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


def place_monitor(bus, connector, side, scale):
    """Put `connector` on `side` of the primary monitor and set its scale.

    Mutter applies a whole layout at once, so every other monitor is re-sent
    exactly as GetCurrentState reports it: same mode, scale, transform,
    primary flag, position (relative to each other) and per-monitor settings
    such as underscan and HDR colour mode. The layout is then shifted so its
    top-left is 0,0, which Mutter requires. Nothing is sent if the virtual
    monitor is already where it should be. Returns True if a change was sent.
    """
    serial, monitors, logicals, props = display_state(bus)
    physical = props.get("layout-mode", 1) == 2

    current = {}            # connector -> (mode_id, w, h, scales, apply props)
    for spec, modes, mprops in monitors:
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
        return False

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
    return True


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

    Mutter takes coordinates in the stream's own pixel space (the virtual
    monitor's width x height), so fractions from the phone are scaled by the
    monitor size we asked for.

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
        self._width = width
        self._height = height
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

    def _to_pixels(self, x, y):
        x = min(max(x, 0.0), 1.0)
        y = min(max(y, 0.0), 1.0)
        return x * (self._width - 1), y * (self._height - 1)

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
    # The virtual monitor dies with our D-Bus connection, so --keep-output
    # can't keep it: teardown() always runs.
    honors_keep_output = False

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

    @staticmethod
    def missing():
        miss = missing_pipeline_tools()
        if Gio is None:
            miss.append("python-gobject (gi)")
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
        """Start the encoder on our PipeWire node; returns its H.264 stream.

        Safe to call again after stop_capture() (or after gst-launch exited
        on its own, e.g. a renegotiation) within one session: the virtual
        monitor and touch session belong to setup()/teardown(), not to this.
        """
        encoder = choose_encoder(self.a, "GNOME")
        self.stop_capture()                 # never two encoders on one node
        proc, stream = start_encoder(self.node, self.a, encoder)
        self.proc = proc
        # The placement thread gets *this* process: after a restart,
        # self.proc is the next one and the old thread must finish with the
        # old one.
        threading.Thread(target=self._place_when_ready, args=(proc,),
                         daemon=True).start()
        return stream

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
                if error is None:
                    settle_until = settle_until or time.monotonic() + 5
            time.sleep(0.25)
        if settle_until is None and proc.poll() is None:
            log("warning: could not place virtual monitor "
                f"({error or 'it never appeared in DisplayConfig'})")

    def apply_live(self, side, scale, bus=None):
        if not self.connector:
            return False
        error = self._place(side, scale, bus)
        if error:
            log(f"warning: could not place virtual monitor ({error})")
        return error is None

    def _place(self, side, scale, bus=None):
        """place_monitor under the lock; returns None or the error."""
        try:
            with self._place_lock:
                changed = place_monitor(
                    bus or Gio.bus_get_sync(Gio.BusType.SESSION, None),
                    self.connector, side, scale)
        except Exception as e:
            return e
        if changed:
            log(f"placed {self.connector} side={side} scale={scale}")
        return None

    def stop_capture(self):
        """Stop the encoder only (the virtual monitor stays). Idempotent."""
        proc, self.proc = self.proc, None
        stop_pipeline(proc)

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
