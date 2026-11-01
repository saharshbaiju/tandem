"""
xdph.py - zero-copy capture of a Hyprland output on NVIDIA, through
xdg-desktop-portal-hyprland (xdph) and GStreamer.

Why: when Hyprland renders on an NVIDIA GPU, wf-recorder can't share frames
with NVENC on the GPU and falls back to wl_shm. Hyprland then copies every
frame into shared memory with a synchronous GPU readback on its render
thread, which is what made the *main* screen drop frames while streaming.
xdph asks Hyprland for DMA-BUF frames instead (a GPU-to-GPU blit), and
GStreamer hands them to NVENC without leaving the GPU:

    xdph ScreenCast session for the output   -> PipeWire node (DMA-BUF frames)
      -> pipewiresrc -> glupload -> glcolorconvert (NV12, on the GPU)
      -> nvh264enc (NVENC, zero latency, no B-frames) -> h264parse
      -> raw Annex-B H.264 on a pipe -> tandemd's parser

No picker: we call xdph's backend interface (org.freedesktop.impl.portal.
ScreenCast) directly with restore data that names the output, which xdph
accepts without prompting ("restore data valid, not prompting"). Format:
xdph's getFullRestoreStruct, version 3.

The session must be closed before the output is removed. Its D-Bus path is
written to SESSION_FILE so a later run (or the TUI's disconnect) can close a
session that a killed daemon left open.
"""

import itertools
import os
import sys
import time

from . import gnome   # shared GStreamer probing helpers

try:
    from gi.repository import Gio, GLib
except Exception:              # reported by available()
    Gio = GLib = None

XDPH_BUS = "org.freedesktop.impl.portal.desktop.hyprland"
XDPH_PATH = "/org/freedesktop/portal/desktop"
SCREENCAST_IFACE = "org.freedesktop.impl.portal.ScreenCast"
SESSION_IFACE = "org.freedesktop.impl.portal.Session"

SOURCE_MONITOR = 1             # ScreenCast source type
CURSOR_EMBEDDED = 2            # draw the pointer into the frames
PERSIST_UNTIL_REVOKED = 2

SESSION_FILE = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"),
                            "tandem-xdph-session")

# Elements the zero-copy pipeline needs (GL import + NVENC from GL memory).
ELEMENTS = ("pipewiresrc", "glupload", "glcolorconvert", "nvh264enc",
            "h264parse")


# Session and request object paths must be unique on xdph's bus. A pid plus
# a per-process counter stays unique when capture is restarted mid-session
# (a timestamp alone repeats within the same second).
_serial = itertools.count(1)


def log(*a):
    try:                       # never raise on a closed terminal / TUI pipe
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _bus():
    return Gio.bus_get_sync(Gio.BusType.SESSION, None)


def unavailable_reason():
    """Why the xdph + GStreamer path can't be used here, or None if it can."""
    if Gio is None:
        return "python3-gobject is not installed"
    missing = [e for e in ELEMENTS if not gnome.gst_inspect(e)]
    if missing:
        return "GStreamer is missing " + ", ".join(missing)
    try:
        owner = _bus().call_sync(
            "org.freedesktop.DBus", "/org/freedesktop/DBus",
            "org.freedesktop.DBus", "NameHasOwner",
            GLib.Variant("(s)", (XDPH_BUS,)), GLib.VariantType("(b)"),
            Gio.DBusCallFlags.NONE, 2000, None).unpack()[0]
    except Exception as e:
        return f"no D-Bus session ({e})"
    if not owner:
        # D-Bus activation would start it, but a portal that isn't running
        # usually means it isn't installed or configured.
        return "xdg-desktop-portal-hyprland is not running"
    return None


class ScreenCastSession:
    """An xdph screencast of one output; `node` is its PipeWire node id."""

    def __init__(self, output):
        self.bus = _bus()
        tag = f"tandem{os.getpid()}_{int(time.time())}_{next(_serial)}"
        self.path = f"{XDPH_PATH}/session/tandem/{tag}"
        self._call("CreateSession", "(oosa{sv})", (self.path, "", {}))
        restore = GLib.Variant("(suv)", ("hyprland", 3, GLib.Variant("a{sv}", {
            "output": GLib.Variant("s", output),
            "withCursor": GLib.Variant("u", CURSOR_EMBEDDED),
            "timeIssued": GLib.Variant("t", int(time.time())),
            "token": GLib.Variant("s", tag),
        })))
        self._call("SelectSources", "(oosa{sv})", (self.path, "", {
            "types": GLib.Variant("u", SOURCE_MONITOR),
            "cursor_mode": GLib.Variant("u", CURSOR_EMBEDDED),
            "restore_data": restore,
            "persist_mode": GLib.Variant("u", PERSIST_UNTIL_REVOKED),
        }))
        results = self._call("Start", "(oossa{sv})", (self.path, "", "", {}))
        streams = results.get("streams") or []
        if not streams:
            self.close()
            raise RuntimeError(f"xdph returned no stream for {output}")
        self.node = streams[0][0]
        try:
            with open(SESSION_FILE, "w") as f:
                f.write(self.path)
        except OSError:
            pass

    def _call(self, method, signature, args):
        """Call an xdph ScreenCast method; returns its results dict."""
        handle = f"{XDPH_PATH}/request/tandem/{os.getpid()}_{next(_serial)}"
        code, results = self.bus.call_sync(
            XDPH_BUS, XDPH_PATH, SCREENCAST_IFACE, method,
            GLib.Variant(signature, (handle,) + tuple(args)),
            GLib.VariantType("(ua{sv})"), Gio.DBusCallFlags.NONE,
            10000, None).unpack()
        if code != 0:
            raise RuntimeError(f"xdph {method} refused (code {code})")
        return results

    def close(self):
        close_session(self.path)


def close_session(path):
    """Close an xdph session by its D-Bus path (it stops capturing)."""
    try:
        _bus().call_sync(XDPH_BUS, path, SESSION_IFACE, "Close", None, None,
                         Gio.DBusCallFlags.NONE, 3000, None)
    except Exception:
        pass                   # already gone
    try:
        with open(SESSION_FILE) as f:
            if f.read().strip() == path:
                os.remove(SESSION_FILE)
    except OSError:
        pass


def close_leftover_session():
    """Close the session a killed daemon may have left capturing an output.

    Called before the output is removed: removing an output that is still
    being captured is the path that can freeze Hyprland.
    """
    if Gio is None:
        return
    try:
        with open(SESSION_FILE) as f:
            path = f.read().strip()
    except OSError:
        return
    if path:
        log("closing a leftover screencast session")
        close_session(path)


def build_pipeline(node, kbps, gop, fd, fps=60):
    """gst-launch argv: DMA-BUF frames of `node` -> NVENC -> Annex-B on fd.

    Every property beyond the basics is probed, so an older nvcodec plugin
    gets the subset it understands instead of failing to parse (which would
    push capture back onto the slow wl_shm path).
    """
    enc = gnome._with_props(
        "nvh264enc", "rc-mode=cbr", f"bitrate={kbps}", f"gop-size={gop}",
        bframes=0,                         # no reorder delay
        zerolatency="true",
        rc_lookahead=0,
        # A VBV of a couple of frames keeps IDRs from bursting the link.
        vbv_buffer_size=gnome.vbv_kbits(kbps, fps),
        # SPS/PPS before every IDR; h264parse config-interval=-1 below
        # covers plugins without this.
        repeat_sequence_header="true")
    if gnome.has_prop("nvh264enc", "tune"):
        enc += " preset=p1 tune=ultra-low-latency"     # GStreamer 1.24+
    else:
        enc += " preset=low-latency-hp"
    desc = (f"{gnome.pipewiresrc(node)} "
            f"! video/x-raw(memory:DMABuf) "
            f"! {gnome.LEAKY_QUEUE} "
            f"! glupload ! glcolorconvert "
            f"! video/x-raw(memory:GLMemory),format=NV12 "
            f"! {enc} "
            f"! h264parse config-interval=-1 "
            f"! video/x-h264,stream-format=byte-stream,alignment=au "
            f"! fdsink fd={fd} sync=false")
    return gnome.gst_launch(desc)
