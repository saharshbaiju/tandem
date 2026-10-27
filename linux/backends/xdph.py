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


def log(*a):
    print("[tandemd]", *a, file=sys.stderr, flush=True)


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
        tag = f"tandem{os.getpid()}_{int(time.time())}"
        self.path = f"{XDPH_PATH}/session/tandem/{tag}"
        self._request = 0
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
        self._request += 1
        handle = f"{XDPH_PATH}/request/tandem/{os.getpid()}_{self._request}"
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


def build_pipeline(node, kbps, gop, fd):
    """gst-launch argv: DMA-BUF frames of `node` -> NVENC -> Annex-B on fd."""
    enc = ["nvh264enc", "rc-mode=cbr", f"bitrate={kbps}", f"gop-size={gop}",
           "bframes=0", "zerolatency=true", "repeat-sequence-header=true"]
    if gnome.has_prop("nvh264enc", "tune"):
        enc += ["preset=p1", "tune=ultra-low-latency"]     # GStreamer 1.24+
    else:
        enc += ["preset=low-latency-hp"]
    # keepalive-time: Hyprland only sends frames on damage; re-send the last
    # one every second so a newly joined phone isn't left on a black screen.
    desc = (f"pipewiresrc path={node} do-timestamp=true keepalive-time=1000 "
            f"! video/x-raw(memory:DMABuf) "
            f"! queue max-size-buffers=1 leaky=downstream "
            f"! glupload ! glcolorconvert "
            f"! video/x-raw(memory:GLMemory),format=NV12 "
            f"! {' '.join(enc)} "
            f"! h264parse config-interval=-1 "
            f"! video/x-h264,stream-format=byte-stream,alignment=au "
            f"! fdsink fd={fd} sync=false")
    return ["gst-launch-1.0", "-q", "-e"] + desc.split()
