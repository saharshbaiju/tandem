"""
hyprland.py - Hyprland backend: headless output + wf-recorder capture.

Pipeline:
    hyprctl output create headless EXT-1   (GPU-backed virtual monitor)
        -> wf-recorder (wlr-screencopy + VA-API H.264, zero-copy on the iGPU)
        -> raw Annex-B H.264 on a pipe -> tandemd's parser

When Hyprland renders on NVIDIA, wf-recorder can only capture through shared
memory, which costs Hyprland a GPU readback per frame on its render thread
(main-screen stutter). There the capture goes through xdph + GStreamer +
NVENC instead, all on the GPU; see xdph.py. wf-recorder stays the fallback.

Touch input: a uinput touchscreen ("tandem-touch", see uinput_touch.py) that
Hyprland maps onto EXT-1, so touches land on the phone's monitor and the mouse
cursor stays where it is.

Golden rule: Tandem only ever writes rules for its *own* output (and the one
workspace it hosts). It never sends a monitor rule for the user's monitors:
a runtime rule replaces the user's whole rule, so even a "position only" rule
resets their mode, refresh, scale, transform, bit depth and VRR. Placement
uses Hyprland's own `auto-<side>` instead, which is drift-free because the
headless output is the last monitor Hyprland arranges (see place_output).

Hyprland 0.55 replaced hyprlang with a Lua config, where `hyprctl keyword` and
`hyprctl dispatch` no longer work. Every state change goes through hypr(),
which detects the dialect once (dialect()) and sends either `hyprctl eval
'<lua>'` or the legacy keyword/dispatch form.
"""

import glob
import json
import os
import re
import select
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time

import uinput_touch
# Re-exported: other modules used to reach the uinput rule through here.
from uinput_touch import (  # noqa: F401
    UINPUT_MODULE_PATH, UINPUT_RULE, UINPUT_RULE_PATH, uinput_setup_problem)

from . import gnome, xdph
# Re-exported: valid_scale lived here before layout.py was shared.
from .layout import valid_scale  # noqa: F401

# The phone gets its own workspace (an id above every one the user has, so it
# never takes over theirs); EXT opens on it because the rule is bound with
# default=true *before* the output is created.
FIRST_EXT_WORKSPACE = 11

# How long to wait for Hyprland to apply a monitor rule. The Lua API only
# schedules the reload for the next frame, so the layout is read back after.
SETTLE_TIMEOUT = 1.5


def log(*a):
    print("[tandemd]", *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# hyprctl plumbing
# --------------------------------------------------------------------------- #

def _instance_dirs():
    """Hyprland instance directories (runtime dir first, pre-0.40 /tmp last)."""
    run = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return glob.glob(f"{run}/hypr/*/") + glob.glob("/tmp/hypr/*/")


_hypr_env = None


def _listening(path):
    """True if a Hyprland answers on this control socket. A crashed session
    leaves its directory and socket file behind; those must not count."""
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            s.connect(path)
        return True
    except OSError:
        return False


def hypr_env():
    """Environment for hyprctl: ours, plus the instance signature if missing.

    Started over ssh, from cron or a systemd --user unit, the session's
    HYPRLAND_INSTANCE_SIGNATURE is not inherited and every hyprctl call fails.
    If exactly one Hyprland instance is running, talk to that one.
    """
    global _hypr_env
    if _hypr_env is None:
        env = dict(os.environ)
        if not env.get("HYPRLAND_INSTANCE_SIGNATURE"):
            sigs = sorted({os.path.basename(d.rstrip("/"))
                           for d in _instance_dirs()
                           if _listening(d + ".socket.sock")})
            if len(sigs) == 1:
                env["HYPRLAND_INSTANCE_SIGNATURE"] = sigs[0]
            elif sigs:
                log("warning: several Hyprland sessions are running and "
                    "HYPRLAND_INSTANCE_SIGNATURE is unset; run tandem inside "
                    "the Hyprland session")
        _hypr_env = env
    return _hypr_env


def hyprctl(*args):
    return subprocess.run(["hyprctl", *args], capture_output=True, text=True,
                          timeout=5, env=hypr_env())


def _ok(r):
    """hyprctl prints `ok` (or `error: ...` / `unknown request`) on success."""
    out = (r.stdout or "").strip().lower()
    return r.returncode == 0 and out.startswith("ok") and "error" not in out


def query(*what):
    """Parsed `hyprctl <what> -j`, or [] if Hyprland can't be read."""
    try:
        return json.loads(hyprctl(*what, "-j").stdout)
    except Exception:
        return []


def monitors():
    return query("monitors")


_dialect = None


def dialect():
    """'lua' (Hyprland >= 0.55, `hyprctl eval`) or 'legacy' (keyword/dispatch).

    Probed once with a no-op Lua chunk. Only a definite answer is cached, so a
    Hyprland that is momentarily unreachable doesn't pin the wrong dialect.
    """
    global _dialect
    if _dialect:
        return _dialect
    try:
        if _ok(hyprctl("eval", "local _ = 1")):
            _dialect = "lua"
        elif query("version"):          # Hyprland answered: no eval -> legacy
            _dialect = "legacy"
    except (OSError, subprocess.SubprocessError):
        pass
    return _dialect or "legacy"


def lua_str(s):
    """A Lua string literal (JSON's escapes are valid Lua for these names)."""
    return json.dumps(str(s), ensure_ascii=False)


def hypr(lua, *legacy):
    """Run one state change in the running Hyprland's dialect; True on success.

    `lua` is a Lua chunk for `hyprctl eval`, `legacy` the hyprctl argv for
    Hyprland < 0.55 (e.g. "keyword", "monitor", "..."). Failures are returned,
    never raised: callers decide whether a failure matters.
    """
    try:
        r = hyprctl("eval", lua) if dialect() == "lua" else hyprctl(*legacy)
    except (OSError, subprocess.SubprocessError):
        return False
    return _ok(r)


# --------------------------------------------------------------------------- #
# Layout geometry (read-only)
# --------------------------------------------------------------------------- #

def logical_size(m):
    """(width, height) of a monitor in layout (logical) pixels.

    hyprctl reports the mode's pixel size *before* the transform, so a rotated
    monitor (transform 1/3/5/7) is swapped. Hyprland snaps fractional scales
    so the logical size is whole (1.5 on 3392 px becomes 1.6 -> 2120), so the
    live scale is read back rather than trusting the requested one.
    """
    sc = m.get("scale", 1) or 1
    w, h = int(round(m["width"] / sc)), int(round(m["height"] / sc))
    if int(m.get("transform", 0) or 0) % 2 == 1:
        w, h = h, w
    return w, h


def rect(m):
    """(x, y, w, h) of a monitor in the layout."""
    w, h = logical_size(m)
    return int(m.get("x", 0)), int(m.get("y", 0)), w, h


def in_layout(m, own):
    """True for monitors that take part in the layout, other than ours."""
    mirror = str(m.get("mirrorOf", "none") or "none")
    return (m.get("name") != own and not m.get("disabled")
            and mirror in ("none", "", "-1"))


def is_virtual(m):
    """Headless/virtual outputs (ours, Sunshine's, wayvnc's) have no EDID."""
    return (str(m.get("name", "")).startswith("HEADLESS-")
            or not str(m.get("description", "")).strip())


def primary_for(name):
    """The main monitor: deterministic, never "wherever the cursor is".

    A physical, enabled, non-mirroring monitor; the one at the layout origin
    if there is one, else the lowest id. Used as the focus target when EXT
    goes away and for the hardware screen; placement doesn't need an anchor.
    """
    mons = [m for m in monitors() if in_layout(m, name)]
    mons = [m for m in mons if not is_virtual(m)] or mons
    if not mons:
        return None
    at_origin = [m for m in mons if (m.get("x"), m.get("y")) == (0, 0)]
    return min(at_origin or mons, key=lambda m: m.get("id", 0))


def _overlap(a0, a1, b0, b1):
    return min(a1, b1) - max(a0, b0)


def placement_problem(name, side, mons=None):
    """Why EXT is badly placed on `side` (overlap / gap / wrong side), or "".

    Correct = it touches no other monitor's area, sits beyond every monitor on
    `side`, and shares an edge with at least one, so the cursor can cross.
    """
    mons = monitors() if mons is None else mons
    me = next((m for m in mons if m.get("name") == name), None)
    others = [rect(m) for m in mons if in_layout(m, name)]
    if not me:
        return f"{name} is missing"
    if not others:
        return ""
    ex, ey, ew, eh = rect(me)
    for x, y, w, h in others:
        if _overlap(ex, ex + ew, x, x + w) > 0 and _overlap(ey, ey + eh, y, y + h) > 0:
            return "overlaps another monitor"
    # (edge of EXT that faces the layout, matching edge of each other monitor,
    #  overlap along the shared edge) per side
    if side == "left":
        edges = [(ex + ew, x, _overlap(ey, ey + eh, y, y + h)) for x, y, w, h in others]
        beyond = all(ex + ew <= x for x, *_ in others)
    elif side == "up":
        edges = [(ey + eh, y, _overlap(ex, ex + ew, x, x + w)) for x, y, w, h in others]
        beyond = all(ey + eh <= y for _, y, _, _ in others)
    elif side == "down":
        edges = [(ey, y + h, _overlap(ex, ex + ew, x, x + w)) for x, y, w, h in others]
        beyond = all(ey >= y + h for _, y, _, h in others)
    else:
        edges = [(ex, x + w, _overlap(ey, ey + eh, y, y + h)) for x, y, w, h in others]
        beyond = all(ex >= x + w for x, _, w, _ in others)
    if not beyond:
        return f"not {side} of every monitor"
    if not any(abs(a - b) <= 1 and span > 0 for a, b, span in edges):
        return "does not touch any monitor"
    return ""


# --------------------------------------------------------------------------- #
# Our own output: mode, placement, workspace, touch
# --------------------------------------------------------------------------- #

def set_monitor(name, mode, position, scale):
    """Apply a complete rule to *our* output; returns True on success.

    Never call this for a user's monitor (see the module docstring).
    """
    lua = (f"hl.monitor({{ output = {lua_str(name)}, mode = {lua_str(mode)}, "
           f"position = {lua_str(position)}, scale = {scale}, transform = 0 }})")
    return hypr(lua, "keyword", "monitor",
                f"{name},{mode},{position},{scale},transform,0")


def _settled_problem(name, width, height, scale, side):
    """Wait for Hyprland to apply our rule; returns placement_problem() then.

    The Lua API applies rules on a later frame, not synchronously, so a quick
    read can still show the *old* layout (two equal reads prove nothing: the
    old layout is stable too). So poll until EXT shows the requested mode and
    scale and sits correctly on `side`, and only judge the placement as bad
    once SETTLE_TIMEOUT has passed. The good case returns at once; only a bad
    placement costs the full timeout.
    """
    end = time.time() + SETTLE_TIMEOUT
    while True:
        time.sleep(0.05)
        mons = monitors()
        me = next((m for m in mons if m.get("name") == name), None)
        applied = (me and (me.get("width"), me.get("height")) == (width, height)
                   and abs(float(me.get("scale", 1) or 1) - scale) < 0.01)
        problem = placement_problem(name, side, mons)
        if (applied and not problem) or time.time() >= end:
            return problem


def place_output(name, width, height, fps, scale, side):
    """Give our output its mode/scale and put it on `side`; True on success.

    Uses `auto-<side>`. Hyprland lays out explicitly positioned monitors
    first, then the auto ones in creation order, each against the bounding
    box of everything placed before it. The headless output is created after
    the user's monitors, so it is placed last and can never push one of them
    (an *explicit* EXT position did: the user's auto-placed monitors are
    arranged after it and moved out of its way, then everything leapfrogged).
    Negative coordinates (left/up) are fine for Hyprland.

    `auto-<side>` aligns EXT with the layout's top/left edge, which in some
    multi-monitor layouts leaves it touching no monitor; then
    `auto-center-<side>` (Hyprland >= 0.51) is tried. Hyprland < 0.46 did not
    count auto-right monitors when placing `auto-down`, so EXT can land on top
    of the main screen there; it then goes to `auto-right` (correct on every
    version) with a warning. Every attempt only moves EXT, and the result is
    read back, never assumed.
    """
    mode = f"{width}x{height}@{fps}"
    tried = []                                   # (position, problem)
    for pos in (f"auto-{side}", f"auto-center-{side}"):
        if not set_monitor(name, mode, pos, scale):
            if not tried:
                return False                     # the mode itself failed
            continue                             # no center variant here
        problem = _settled_problem(name, width, height, scale, side)
        if not problem:
            return True
        tried.append((pos, problem))
    usable = [pos for pos, problem in tried if "overlap" not in problem]
    if usable:
        pos = usable[0]
        log(f"note: {name} at {pos} "
            f"{dict(tried)[pos]}; your monitors were left as they are")
    else:
        pos = "auto-right"
        log(f"warning: {name} {tried[0][1]} at auto-{side} (Hyprland older "
            "than 0.46 cannot place it there without moving your monitors); "
            "placing it to the right instead")
    if pos != tried[-1][0]:
        set_monitor(name, mode, pos, scale)
        _settled_problem(name, width, height, scale,
                         "right" if pos == "auto-right" else side)
    return True


def apply_live(name, width, height, fps, scale, side):
    """Re-apply mode/scale/side to an existing output (no stream restart)."""
    return place_output(name, width, height, fps,
                        valid_scale(width, height, scale), side)


def pick_ext_workspace(name):
    """The workspace id the phone hosts: the one it shows now, else a free id
    above every workspace and numeric workspace rule the user has."""
    # numeric workspaces the user's own rules bind to one of their monitors
    user_ruled = set()
    for r in query("workspacerules"):
        ws = str(r.get("workspaceString", ""))
        if ws.isdigit() and r.get("monitor") not in (None, "", name):
            user_ruled.add(int(ws))
    me = next((m for m in monitors() if m.get("name") == name), None)
    if me:
        ws = (me.get("activeWorkspace") or {}).get("id")
        # Keep what EXT shows (reused output), unless it is a workspace the
        # user's config binds elsewhere: our default rule would override it.
        if isinstance(ws, int) and ws > 0 and ws not in user_ruled:
            return ws
    used = [w.get("id", 0) for w in query("workspaces")
            if w.get("monitor") != name] + list(user_ruled)
    return max([FIRST_EXT_WORKSPACE - 1] + [u for u in used if u > 0]) + 1


def _ws_registry():
    """Lua global keeping the workspace rule objects we created, so any
    process (daemon or TUI) can disable them later. A config reload resets the
    Lua state, and with it the rules, so a missing registry means "nothing
    to undo"."""
    return "_G.__tandem_ws_rules = _G.__tandem_ws_rules or {}"


def bind_workspace(name, ws):
    """Make workspace `ws` live on (and be the default of) our output only."""
    lua = (f"{_ws_registry()}\n"
           f"local key = {lua_str(name)}\n"
           "local old = __tandem_ws_rules[key]\n"
           "if old then pcall(function() old:set_enabled(false) end) end\n"
           f"__tandem_ws_rules[key] = hl.workspace_rule({{ workspace = "
           f"{lua_str(ws)}, monitor = key, default = true }})")
    ok = hypr(lua, "keyword", "workspace", f"{ws},monitor:{name},default:true")
    log(f"workspace {ws} -> {name}" + ("" if ok else " (could not bind)"))
    return ok


def unbind_workspace(name):
    """Disable the workspace rule bind_workspace made (Lua). Legacy Hyprland
    has no rule removal; that rule names an output that no longer exists, so
    it is inert, and gone at the next config reload."""
    if dialect() != "lua":
        return
    hypr(f"{_ws_registry()}\n"
         f"local old = __tandem_ws_rules[{lua_str(name)}]\n"
         "if old then pcall(function() old:set_enabled(false) end) end\n"
         f"__tandem_ws_rules[{lua_str(name)}] = nil")


def map_touch_device(device, output):
    """Confine an absolute touch device to one monitor (instead of the whole
    desktop layout). Re-applied after config reloads, which drop it."""
    return hypr(f"hl.device({{ name = {lua_str(device)}, "
                f"output = {lua_str(output)} }})",
                "keyword", f"device[{device}]:output", output)


def focus_monitor(target):
    hypr(f"hl.dispatch(hl.dsp.focus({{ monitor = {lua_str(target)} }}))",
         "dispatch", "focusmonitor", target)


def move_workspace(ws, target):
    """Move a workspace (a `hyprctl workspaces` entry) to monitor `target`.

    Named workspaces have negative ids, which no dispatcher accepts as a
    number, so they are addressed as `name:<name>`.
    """
    sel = str(ws["id"]) if ws.get("id", 0) > 0 else f"name:{ws.get('name')}"
    hypr(f"hl.dispatch(hl.dsp.workspace.move({{ workspace = "
         f"{lua_str(sel)}, monitor = {lua_str(target)} }}))",
         "dispatch", "moveworkspacetomonitor", sel, target)


def create_output(name):
    """`hyprctl output create headless <name>`, checked; raises on failure.

    Waits for the output to really appear. If Hyprland made one under another
    name (very old builds ignore the name), that stray HEADLESS-* is removed
    again so it doesn't linger as an invisible monitor.
    """
    before = {m.get("name") for m in monitors()}
    r = hyprctl("output", "create", "headless", name)
    end = time.time() + 2.0
    while time.time() < end:
        names = {m.get("name") for m in monitors()}
        if name in names:
            return
        time.sleep(0.1)
    for stray in names - before:
        if str(stray).startswith("HEADLESS-"):
            hyprctl("output", "remove", stray)
    reply = (r.stdout or r.stderr or "").strip() or f"exit {r.returncode}"
    raise RuntimeError(f"Hyprland did not create the {name} output ({reply})")


def remove_output(name):
    """Safely take the virtual output away, in an order Hyprland tolerates.

    1. stop anything still capturing it (a pending screencopy frame on a
       destroyed output can freeze the compositor),
    2. if it has focus, move focus to the main monitor (removing the focused
       monitor is a crash path),
    3. move its workspaces (and windows) to the main monitor,
    4. remove it, and drop our workspace rule.
    The user's monitors are never touched, so there is nothing to restore.
    Safe to call when the output is already gone (cleans up the rule only).
    """
    xdph.close_leftover_session()
    kill_stale_capture(name)
    mons = monitors()
    me = next((m for m in mons if m.get("name") == name), None)
    primary = primary_for(name)
    if me and primary:
        if me.get("focused"):
            focus_monitor(primary["name"])
        for w in query("workspaces"):
            # special workspaces follow the monitor's removal by themselves
            if (w.get("monitor") == name
                    and not str(w.get("name", "")).startswith("special:")):
                move_workspace(w, primary["name"])
        time.sleep(0.1)             # let the moves render before the removal
    if me:
        hyprctl("output", "remove", name)
        log(f"removed headless output {name}")
    unbind_workspace(name)


class EventWatcher:
    """Follows Hyprland's event socket (socket2) and calls `on_change` when
    our runtime rules may have been lost or the layout changed.

    A config reload (autoreload fires on every save of hyprland.conf/.lua, and
    display GUIs write the config) rebuilds all rules from the files: EXT
    would fall back to the catch-all rule (other mode, size, position), the
    touch mapping and our workspace rule would be gone. Monitor hotplug can
    leave EXT off its side. Events are debounced: one burst, one re-apply.
    """

    EVENTS = ("configreloaded", "monitoradded", "monitoraddedv2",
              "monitorremoved", "monitorremovedv2")

    # Events that arrive this soon after our own re-apply are its echo, not a
    # new change; ignoring them rules out a re-apply -> event -> re-apply loop.
    ECHO_WINDOW = 1.0

    def __init__(self, own, on_change, debounce=0.4):
        self.own, self.on_change, self.debounce = own, on_change, debounce
        self._stop = threading.Event()
        self._thread = None
        self._quiet_until = 0.0

    def mute(self, seconds=ECHO_WINDOW):
        """Ignore events for a moment (call after changing our own output)."""
        self._quiet_until = time.time() + seconds

    def _socket_path(self):
        sig = hypr_env().get("HYPRLAND_INSTANCE_SIGNATURE", "")
        run = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        for path in (f"{run}/hypr/{sig}/.socket2.sock",
                     f"/tmp/hypr/{sig}/.socket2.sock"):
            if sig and os.path.exists(path):
                return path
        return None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _relevant(self, line):
        event, _, data = line.partition(">>")
        if event not in self.EVENTS:
            return False
        # our own output appearing/disappearing is not a reason to re-apply
        return event == "configreloaded" or self.own not in data.split(",")

    def _run(self):
        while not self._stop.is_set():
            path = self._socket_path()
            if not path:
                return                   # no event socket: nothing to follow
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.connect(path)
                    s.settimeout(0.5)
                    self._read(s)
            except OSError:
                pass
            self._stop.wait(2.0)         # Hyprland restarting: retry later

    def _read(self, s):
        buf, due = b"", None
        while not self._stop.is_set():
            try:
                chunk = s.recv(4096)
                if not chunk:
                    return
                buf += chunk
            except socket.timeout:
                pass
            *lines, buf = buf.split(b"\n")
            if (time.time() >= self._quiet_until and
                    any(self._relevant(l.decode("utf-8", "replace"))
                        for l in lines)):
                due = time.time() + self.debounce
            if due and time.time() >= due and not self._stop.is_set():
                due = None
                try:
                    self.on_change()
                except Exception as e:
                    log(f"warning: could not re-apply {self.own} ({e})")
                self.mute()


# --------------------------------------------------------------------------- #
# Encoder selection: encode on the GPU Hyprland renders on
#
# wf-recorder can only share frames zero-copy (DMA-BUF) with an encoder on the
# compositor's own GPU; across GPUs it falls back to CPU copies that VA-API
# can't upload ("Unsupported format: bgra"). On hybrid laptops Hyprland's
# render GPU is the first entry of AQ_DRM_DEVICES (else the boot VGA device).
# NVIDIA's VA driver (nvidia-vaapi-driver) only decodes, so NVIDIA -> NVENC.

VA_DRIVERS = {"amdgpu": "radeonsi", "radeon": "radeonsi",
              "i915": "iHD", "xe": "iHD"}


def _card_of(path):
    """'cardN' for a /dev/dri/{cardN,renderDN} path (symlinks resolved)."""
    base = os.path.basename(os.path.realpath(path))
    if base.startswith("card"):
        return base
    for d in glob.glob(f"/sys/class/drm/{base}/device/drm/card*"):
        return os.path.basename(d)
    return None


def gpu_driver(path):
    """Kernel driver name ('nvidia', 'amdgpu', 'i915', ...) of a DRM node."""
    card = _card_of(path)
    try:
        return os.path.basename(os.readlink(f"/sys/class/drm/{card}/device/driver"))
    except (OSError, TypeError):
        return None


def render_node(card):
    """/dev/dri/renderDN belonging to a cardN."""
    for d in sorted(glob.glob(f"/sys/class/drm/{card}/device/drm/renderD*")):
        return "/dev/dri/" + os.path.basename(d)
    return None


def compositor_card():
    """cardN Hyprland renders on (first AQ_DRM_DEVICES entry, else boot VGA)."""
    aq = os.environ.get("AQ_DRM_DEVICES", "").split(":")[0]
    if aq and _card_of(aq):
        return _card_of(aq)
    for vga in sorted(glob.glob("/sys/class/drm/card*/device/boot_vga")):
        try:
            if open(vga).read().strip() == "1":
                return vga.split("/")[4]
        except OSError:
            pass
    return None


# Where distros install VA-API drivers; LIBVA_DRIVERS_PATH (user override)
# and /run/opengl-driver (NixOS) come first.
VA_DIRS = tuple(
    [d for d in os.environ.get("LIBVA_DRIVERS_PATH", "").split(":") if d]
    + ["/run/opengl-driver/lib/dri", "/usr/lib/dri", "/usr/lib64/dri",
       "/usr/lib/x86_64-linux-gnu/dri", "/usr/lib/aarch64-linux-gnu/dri"])


def va_driver_for(node):
    """LIBVA_DRIVER_NAME that can *encode* on this render node, or None."""
    name = VA_DRIVERS.get(gpu_driver(node) or "")
    if name == "iHD" and not va_installed("iHD") and va_installed("i965"):
        name = "i965"                     # pre-Broadwell Intel / old distros
    return name


def va_installed(name):
    return any(os.path.exists(f"{d}/{name}_drv_video.so") for d in VA_DIRS)


def software_encoders():
    """CPU H.264 encoders the system's libavcodec has, best first.

    wf-recorder links the same libavcodec as the ffmpeg binary, so
    `ffmpeg -encoders` tells which exist. Fedora/openSUSE's ffmpeg-free has no
    libx264 but does have libopenh264. Without the ffmpeg binary, both are
    offered and the start check sorts it out.
    """
    wanted = ("libx264", "libopenh264")
    if not shutil.which("ffmpeg"):
        return list(wanted)
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return list(wanted)
    have = {line.split()[1] for line in out.splitlines()
            if len(line.split()) > 1}
    return [e for e in wanted if e in have] or list(wanted)


SOFTWARE_DESC = {"libx264": "x264 on the CPU",
                 "libopenh264": "OpenH264 on the CPU"}


def encoder_candidates(requested, device):
    """Encoders to try, best first: [(ffmpeg encoder, VA node, env, description)].

    auto: the hardware encoder of the GPU Hyprland renders on (zero-copy), then
    a CPU encoder (x264, else OpenH264), which works on any machine (NVIDIA
    without NVENC, nouveau, VMs, a VA driver without H.264 encode, ...).
    """
    # HEVC is chosen per candidate later (start_capture); select by family.
    requested = requested.replace("hevc_", "h264_").replace("libx265", "libx264")
    card = compositor_card()
    drv = gpu_driver(card) if card else None
    out = []
    if requested == "auto" and drv == "nvidia":
        reason = xdph.unavailable_reason()
        if reason:
            log(f"zero-copy NVIDIA capture unavailable ({reason}); "
                "using wf-recorder, which costs Hyprland a copy per frame")
        else:
            out.append((XDPH_NVENC, None, {},
                        f"NVENC on {card} via xdph + GStreamer (zero-copy)"))
    if requested == "auto":
        if drv == "nvidia":
            requested = "h264_nvenc"
        else:
            requested = "h264_vaapi"
            if card and render_node(card):
                device = render_node(card)    # same GPU -> zero-copy
    if requested == "h264_nvenc":
        out.append((requested, None, {}, f"NVENC on {card or 'NVIDIA'}"))
    elif requested == "h264_vaapi":
        va = va_driver_for(device)
        # NVIDIA Hyprland setups often export LIBVA_DRIVER_NAME=nvidia
        # globally; that loads the decode-only driver on an AMD/Intel node.
        out.append((requested, device, {"LIBVA_DRIVER_NAME": va} if va else {},
                    f"VA-API on {device} ({va or 'default driver'})"))
        if va == "iHD" and va_installed("i965"):
            # iHD installed but the GPU predates it (Haswell/Ivy Bridge)
            out.append((requested, device, {"LIBVA_DRIVER_NAME": "i965"},
                        f"VA-API on {device} (i965)"))
    soft = software_encoders()
    if requested in soft:                 # explicitly asked for one: first
        soft.remove(requested)
        soft.insert(0, requested)
    fallback = "" if requested in SOFTWARE_DESC else " (fallback)"
    out += [(e, None, {}, SOFTWARE_DESC[e] + fallback) for e in soft]
    return out


XDPH_NVENC = "xdph+nvh264enc"   # pseudo-encoder: the xdph.py pipeline


def pick_encoder(requested, device):
    """The first choice of encoder_candidates (for display)."""
    return encoder_candidates(requested, device)[0]


def bitrate_bps(bitrate):
    """'12M' / '8000k' / '5000000' -> bits per second."""
    s = str(bitrate).strip().lower()
    mult = {"k": 1_000, "m": 1_000_000, "g": 1_000_000_000}.get(s[-1:], 1)
    return int(float(s.rstrip("kmg") or 0) * mult)


# Rate-control buffer in frames. A VBV of ~2 frames keeps every frame, IDRs
# included, close to the average size, so no frame needs many times the
# link's per-frame budget (a 1 s VBV let IDRs grow to several hundred KB and
# stall the link for the frames behind them). Trade-off: IDRs are softer and
# sharpen over the next frames.
VBV_FRAMES = 2


def keyframe_interval(a):
    """GOP length in frames: the configured one, but at least 1 s of frames.

    Capture is damage-driven, so frames (and IDRs) only flow while something
    changes; a short frame-based GOP meant 2-4 IDR bursts per second of
    motion. Recovery doesn't need them: a phone that loses a frame asks for a
    keyframe (KEYQ) and gets the cached GOP replayed. ~1 s keeps the cached
    GOP well under the per-client queue limit for that replay.
    """
    return max(int(a.gop), int(a.fps))


def encoder_params(encoder, a):
    """wf-recorder -p options for minimum latency on each encoder."""
    bps = bitrate_bps(a.bitrate)
    vbv = max(bps * VBV_FRAMES // max(int(a.fps), 1), 1)
    common = [f"g={keyframe_interval(a)}", "bf=0", f"b={bps}",
              f"maxrate={bps}", f"bufsize={vbv}"]
    if encoder.endswith("_nvenc"):
        return common + ["preset=p1", "tune=ull", "zerolatency=1",
                         "rc=cbr", "delay=0", "forced-idr=1"]
    if encoder.endswith("_vaapi"):
        return common + ["rc_mode=CBR", "async_depth=1"]
    if encoder == "libopenh264":
        # one slice per frame (the phone feeds one buffer per access unit),
        # never drop frames to meet the bitrate
        return common + ["rc_mode=bitrate", "allow_skip_frames=0", "slices=1"]
    # libx264/libx265. tune=zerolatency turns on sliced threads, i.e. one
    # slice per thread, so a frame arrives as many NALs that strict phone
    # decoders take for many frames. One slice per frame instead; frame
    # threads add a frame of delay each, so use one thread unless the pixel
    # rate is above 1080p60 (ultrafast keeps up with that on one core).
    threads = 1 if a.width * a.height * a.fps <= 1920 * 1080 * 60 else 2
    params = (["x264-params=sliced-threads=0:slices=1"]
              if encoder == "libx264" else [])
    return common + ["preset=ultrafast", "tune=zerolatency",
                     f"threads={threads}"] + params


_wf_flags = None


def wf_recorder_flags():
    """Long options this wf-recorder supports (from --help), cached."""
    global _wf_flags
    if _wf_flags is None:
        try:
            r = subprocess.run(["wf-recorder", "--help"], capture_output=True,
                               text=True, timeout=5)
            _wf_flags = set(re.findall(r"--([a-z][a-z-]+)",
                                       (r.stdout or "") + (r.stderr or "")))
        except (OSError, subprocess.SubprocessError):
            _wf_flags = set()
    return _wf_flags


def capture_pids(output):
    """PIDs of every wf-recorder recording `output`."""
    pids = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as f:
                args = [a.decode("utf-8", "replace")
                        for a in f.read().split(b"\x00") if a]
        except OSError:
            continue
        if args and args[0].endswith("wf-recorder") and output in args:
            pids.append(int(entry))
    return pids


def kill_stale_capture(output, grace=2.0):
    """Stop any wf-recorder still recording `output` and wait until it is gone.

    Waiting matters: removing a headless output while a client still has a
    screencopy frame pending on it can freeze Hyprland (the "pressing d hangs
    the system" bug). SIGTERM first, SIGKILL whatever outlives `grace`.
    """
    pids = capture_pids(output)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            log(f"stopping leftover wf-recorder pid {pid}")
        except OSError:
            pass
    deadline = time.time() + grace
    while pids and time.time() < deadline:
        pids = [p for p in pids if os.path.exists(f"/proc/{p}")]
        time.sleep(0.05)
    for pid in pids:
        try:
            os.kill(pid, signal.SIGKILL)
            log(f"killed wf-recorder pid {pid} (ignored SIGTERM)")
        except OSError:
            pass


PR_SET_PDEATHSIG = 1


def _die_with_parent():
    """preexec_fn: have the kernel SIGTERM wf-recorder when the daemon dies.

    Without it a SIGKILLed daemon leaves wf-recorder orphaned, still capturing
    the output the TUI is about to remove.
    """
    try:
        import ctypes
        ctypes.CDLL(None, use_errno=True).prctl(PR_SET_PDEATHSIG,
                                                 signal.SIGTERM)
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# Module hooks (see backends/__init__.py): used without a backend instance
# --------------------------------------------------------------------------- #

def session_confidence():
    """3 when this process is a client of a running Hyprland (its instance
    signature is set); 1 when only XDG_CURRENT_DESKTOP names Hyprland;
    else 0."""
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return 3
    if "hyprland" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return 1
    return 0


def session_problem():
    """None: every Hyprland session is Wayland, and hyprctl reports anything
    else when setup() runs."""
    return None


def describe_primary_monitor():
    """'<name>  WxH@Hz  scale s' for the main monitor, or None."""
    try:
        m = primary_for("EXT-1")
    except Exception:
        return None
    if not m:
        return None
    return (f"{m['name']}  {m['width']}x{m['height']}"
            f"@{m.get('refreshRate', 0):.0f}  scale {m.get('scale', 1)}")


def remove_leftover_output(output_name):
    """Remove a headless output the daemon left behind. The TUI starts the
    daemon with --keep-output so the monitor survives restarts; this takes
    it away (windows moved to the main monitor first). Never raises: it
    runs in the TUI, where a failure has nowhere useful to go (and a log
    line on stderr would land on top of the curses screen)."""
    try:
        remove_output(output_name)
    except Exception:
        pass


def touch_setup_problem():
    """Touch needs a uinput device, which needs a one-time udev rule."""
    return uinput_setup_problem()


class HyprlandBackend:
    name = "hyprland"
    # The headless output can outlive the daemon: the TUI restarts the
    # daemon with --keep-output so the phone's monitor (and the windows on
    # it) survive the restart.
    honors_keep_output = True

    def __init__(self, args):
        self.a = args
        self.proc = None
        self.stream = None          # readable Annex-B byte stream
        self.session = None         # xdph.ScreenCastSession while capturing
        self.workspace = None       # the workspace id the phone hosts
        self._working = None        # encoder candidate that started last time
        self._touch_mapped = False
        # live changes (SIGHUP) and config-reload re-applies come from
        # different threads; one layout change at a time
        self._layout_lock = threading.Lock()
        self._torn_down = False
        self._watcher = EventWatcher(args.output, self._reapply)

    @staticmethod
    def missing():
        return [t for t in ("hyprctl", "wf-recorder") if not shutil.which(t)]

    def setup(self):
        """Create the headless output (or reuse one) and place it on `side`.

        Only our own output and its workspace rule are written; the user's
        monitors and workspaces are left exactly as they are.
        """
        a = self.a
        kill_stale_capture(a.output)
        reuse = any(m.get("name") == a.output for m in monitors())
        log(f"Hyprland config dialect: {dialect()}")
        # Bound before the output exists, so Hyprland opens EXT on this
        # workspace instead of grabbing the lowest free one.
        self.workspace = pick_ext_workspace(a.output)
        bind_workspace(a.output, self.workspace)
        if reuse:
            log(f"reusing existing headless output {a.output}")
        else:
            log(f"creating headless output {a.output}")
            try:
                create_output(a.output)
            except Exception:
                unbind_workspace(a.output)
                raise
        scale = valid_scale(a.width, a.height, a.scale)
        if scale != a.scale:
            log(f"scale {a.scale} gives fractional pixels at {a.width}x"
                f"{a.height}; using {scale:g}")
        with self._layout_lock:
            if not place_output(a.output, a.width, a.height, a.fps, scale,
                                a.side):
                log(f"warning: could not apply mode {a.width}x{a.height}")
        me = next((m for m in monitors() if m.get("name") == a.output), {})
        log(f"output {a.output} ready ({a.width}x{a.height}@{a.fps}, scale "
            f"{scale:g}, at {me.get('x', '?')}x{me.get('y', '?')}, "
            f"side {a.side})")
        self._watcher.start()

    def _reapply(self):
        """After a config reload or a monitor hotplug: put back what is ours
        (mode/scale/side, the workspace rule, the touch mapping)."""
        a = self.a
        if not any(m.get("name") == a.output for m in monitors()):
            return                      # output gone: nothing of ours to fix
        with self._layout_lock:
            if self._torn_down:         # teardown won the lock: don't rebind
                return
            if self.workspace:
                bind_workspace(a.output, self.workspace)
            place_output(a.output, a.width, a.height, a.fps,
                         valid_scale(a.width, a.height, a.scale), a.side)
            if self._touch_mapped:
                map_touch_device(uinput_touch.DEVICE_NAME, a.output)
        log(f"re-applied {a.output} settings after a Hyprland config reload "
            "or monitor change")

    def _candidates(self):
        """Encoder candidates for this run, the one that worked last first
        (a capture restart mid-session shouldn't wait out failing ones)."""
        a = self.a
        req = (a.encoder if a.encoder.startswith(("h264_", "hevc_", "libx26",
                                                  "libopenh264"))
               else "auto")
        cands = encoder_candidates(req, a.device)
        if a.codec == "hevc":            # H.264-only pipelines
            cands = [c for c in cands if c[0] not in (XDPH_NVENC, "libopenh264")]
        if self._working:
            cands.sort(key=lambda c: self._key(c) != self._working)
        return cands

    @staticmethod
    def _key(cand):
        encoder, device, env, _ = cand
        return encoder, device, tuple(sorted(env.items()))

    def start_capture(self):
        """Launch the encoder; returns the readable Annex-B byte stream.

        Tries each encoder candidate in turn: one that can't initialise either
        exits within a moment (wrong GPU, missing driver/NVENC) or hangs without
        output (some Intel VA-API drivers), so we move on to the next instead of
        leaving the phone with a black screen. Raises RuntimeError when none
        works. Safe to call again mid-session (after stop_capture, or after
        the encoder died): the output and its placement are untouched.
        """
        self.stop_capture()
        a = self.a
        cands = self._candidates()
        tried = []
        for i, cand in enumerate(cands):
            encoder, device, env, desc = cand
            if a.codec == "hevc":
                encoder = encoder.replace("h264_", "hevc_").replace("264", "265")
            log(f"encoder: {desc}")
            tried.append(desc)
            try:
                if encoder == XDPH_NVENC:
                    self._spawn_xdph()
                else:
                    self._spawn(encoder, device, env)
            except Exception as e:
                log(f"encoder {encoder} could not start ({e}); trying the next one")
                self.stop_capture()
                continue
            problem = self._fails_to_start()
            # Silent but alive is kept for the last resort, and for the
            # encoder that already worked in this session: on a restart a
            # still screen may simply have nothing new to send, and dropping
            # to a CPU encoder for that would be a lasting downgrade.
            proven = self._key(cand) == self._working
            if not problem or (problem == "silent"
                               and (proven or i == len(cands) - 1)):
                self._working = self._key(cand)
                return self.stream
            self.stop_capture()
            log(f"encoder {encoder} failed to start; trying the next one")
        raise RuntimeError(f"no working {a.codec.upper()} encoder (tried: "
                           f"{'; '.join(tried) or 'none'})")

    def _spawn_xdph(self):
        """Zero-copy NVIDIA capture: an xdph screencast into GStreamer NVENC."""
        a = self.a
        self.session = xdph.ScreenCastSession(a.output)
        rfd, wfd = os.pipe()
        cmd = xdph.build_pipeline(self.session.node,
                                  gnome.bitrate_kbps(a.bitrate),
                                  keyframe_interval(a), wfd, fps=a.fps)
        log("capture cmd: " + " ".join(cmd))
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, pass_fds=(wfd,),
                                     preexec_fn=_die_with_parent)
        os.close(wfd)              # our copy; EOF arrives when gst exits
        self.stream = os.fdopen(rfd, "rb", buffering=0)
        threading.Thread(target=self._wf_log, args=(self.proc,),
                         daemon=True).start()

    def _spawn(self, encoder, device, env):
        a = self.a
        cmd = ["wf-recorder", "-o", a.output, "-c", encoder]
        if device:
            cmd += ["-d", device]
        if not encoder.endswith(("_vaapi", "_nvenc")):
            cmd += ["-x", "yuv420p"]      # phones decode 4:2:0 only
        # no B-frames (no reorder delay), CBR, minimal encoder pipelining
        for p in encoder_params(encoder, a):
            cmd += ["-p", p]
        # Never `-r`: it adds an ffmpeg fps filter that holds the newest frame
        # until the next one arrives and, after an idle gap, emits idle_s*fps
        # duplicates (with IDRs) in one burst: the "freezes when I drag a
        # window onto it" bug. Capture stays damage-driven (variable rate);
        # -B only tells the encoder's rate control the expected rate.
        if "buffrate" in wf_recorder_flags():
            cmd += ["-B", str(a.fps)]
        cmd += [f"--muxer={'hevc' if a.codec == 'hevc' else 'h264'}",
                "-f", "/dev/stdout"]
        log("capture cmd: " + " ".join(cmd))
        # stdin=DEVNULL: wf-recorder otherwise prompts "/dev/stdout exists.
        # Overwrite? Y/n" and blocks forever (or eats the TUI's keystrokes).
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     env={**hypr_env(), **env},
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0,
                                     preexec_fn=_die_with_parent)
        self.stream = self.proc.stdout
        threading.Thread(target=self._wf_log, args=(self.proc,),
                         daemon=True).start()

    def _fails_to_start(self, grace=4.0):
        """None if the encoder works, else "exited" or "silent".

        A working encoder emits its first access unit in ~0.1 s; one still
        silent after seconds is stuck (seen with VA-API on Intel) and would
        leave the phone on a black screen.
        """
        p = self.proc
        end = time.time() + grace
        while time.time() < end:
            if p.poll() is not None:
                return "exited"
            r, _, _ = select.select([self.stream], [], [], 0.1)
            if r:      # bytes waiting (or EOF): alive a moment later = works
                time.sleep(0.2)
                return "exited" if p.poll() is not None else None
        log(f"no output after {grace:g}s")
        return "silent"

    def _wf_log(self, proc):
        """Forward the encoder's diagnostics (they explain why capture died)."""
        tag = "gst: " if self.session else "wf: "
        for raw in proc.stderr:
            # progress lines are \r-separated; keep only the last segment
            line = raw.decode("utf-8", "replace").split("\r")[-1].strip()
            if line:
                log(tag + line)

    def create_touch_surface(self):
        """A uinput touchscreen whose whole area is the phone's monitor."""
        problem = uinput_touch.uinput_access_problem()
        if problem:
            raise RuntimeError(f"{problem}; run tandem on the computer and "
                               "press t to set up touch")
        screen = uinput_touch.VirtualTouchscreen(self.a.width, self.a.height)
        self._touch_mapped = True        # re-mapped after config reloads
        if not map_touch_device(uinput_touch.DEVICE_NAME, self.a.output):
            log("warning: could not map the touch device to "
                f"{self.a.output}; touches may land on the wrong monitor")
        screen.description += f" mapped to {self.a.output}"
        return screen

    def apply_live(self, side, scale):
        a = self.a
        with self._layout_lock:
            if self._touch_mapped:
                map_touch_device(uinput_touch.DEVICE_NAME, a.output)
            ok = apply_live(a.output, a.width, a.height, a.fps, scale, side)
        self._watcher.mute()            # our own change, not the user's
        log(f"moved {a.output} side={side} scale={scale}")
        return ok

    def stop_capture(self):
        """Stop the encoder, then end the screencast (before any removal).

        Idempotent; the output stays, so start_capture can run again.
        """
        p, self.proc = self.proc, None
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if p and p.stdout:
            p.stdout.close()            # don't leak a pipe per restart
        if self.stream and self.stream is not getattr(p, "stdout", None):
            try:
                self.stream.close()     # our end of the GStreamer pipe
            except OSError:
                pass
        self.stream = None
        if self.session:
            self.session.close()
            self.session = None

    def teardown(self):
        self._watcher.stop()
        # Under the layout lock: a re-apply already running would otherwise
        # re-bind the workspace rule after remove_output dropped it.
        with self._layout_lock:
            self._torn_down = True
            remove_output(self.a.output)
