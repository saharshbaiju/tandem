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
Hyprland maps onto EXT-1 with `hl.device({ name, output })`, so touches land on
the phone's monitor and the mouse cursor stays where it is.

Hyprland 0.55 replaced hyprlang with a Lua config parser, so `hyprctl keyword`
and `hyprctl dispatch` silently fail there. Everything that changes monitors or
workspaces goes through `hyprctl eval '<lua>'` first and only falls back to the
legacy keyword form for older Hyprland.
"""

import glob
import json
import os
import select
import shutil
import signal
import subprocess
import sys
import threading
import time

import uinput_touch

from . import gnome, xdph

# Workspace split: the laptop keeps LAPTOP_WS, the phone hosts EXT_WS, so the
# secondary monitor behaves as a true extension. Bound as rules *before* the
# output is created, so Hyprland opens EXT on its default workspace (first of
# EXT_WS) instead of grabbing the lowest free one.
LAPTOP_WS = range(1, 11)     # workspaces 1..10 stay on the main display
EXT_WS = range(11, 21)       # workspaces 11..20 live on the phone


def log(*a):
    print("[tandemd]", *a, file=sys.stderr, flush=True)


def hyprctl(*args):
    return subprocess.run(["hyprctl", *args], capture_output=True, text=True,
                          timeout=5)


def monitors():
    try:
        return json.loads(hyprctl("monitors", "-j").stdout)
    except Exception:
        return []


def logical_size(m):
    """(width, height) of a monitor in logical pixels (physical / scale).

    Hyprland snaps fractional scales to keep logical pixels integer (scale 1.5
    on a 3392px output becomes 1.6 -> 2120 logical), so we read the snapped
    size back rather than trusting width/scale.
    """
    sc = m.get("scale", 1) or 1
    return int(round(m["width"] / sc)), int(round(m["height"] / sc))


def primary_for(name):
    """The main monitor to anchor against: focused non-EXT, else first non-EXT."""
    mons = [m for m in monitors() if m.get("name") != name]
    return (next((m for m in mons if m.get("focused")), None)
            or (mons[0] if mons else None))


def set_monitor(name, mode, position, scale):
    """Apply a monitor mode/position/scale at runtime, returns True on success.

    Uses the Lua `hl.monitor()` API via `hyprctl eval` (Hyprland >= 0.55),
    falling back to the legacy `keyword monitor` form for older versions.
    `hyprctl eval` prints `ok` / `error: ...`, so we check stdout too.
    """
    lua = (f'hl.monitor({{ output = "{name}", mode = "{mode}", '
           f'position = "{position}", scale = {scale} }})')
    r = hyprctl("eval", lua)
    out = (r.stdout or "").lower()
    if r.returncode == 0 and "ok" in out and "error" not in out:
        return True
    rule = f"{name},{mode},{position},{scale}"
    return hyprctl("keyword", "monitor", rule).returncode == 0


def pin_primary(primary):
    """Pin the primary at its current position so Hyprland stops auto-placing it.

    The primary is usually `monitor=,preferred,auto,auto`. With `auto`
    placement Hyprland recomputes its position every time another output moves,
    so the two monitors leapfrog and `side` never settles. Restored to auto on
    teardown (see restore_primary).
    """
    if not primary:
        return
    x, y = int(primary.get("x", 0)), int(primary.get("y", 0))
    hyprctl("eval", f'hl.monitor({{ output = "{primary["name"]}", '
                    f'position = "{x}x{y}" }})')


def restore_primary(name):
    """Hand the primary back to Hyprland's auto placement (undo pin_primary)."""
    pr = primary_for(name)
    if pr:
        hyprctl("eval", f'hl.monitor({{ output = "{pr["name"]}", '
                        f'position = "auto" }})')


def flush_position(name, side, fallback):
    """Logical coords that butt `name` flush against the primary on `side`.

    Uses the *live* logical sizes so scale snapping can't leave a gap/overlap at
    the seam. Falls back to the caller's estimate if the layout can't be read.
    """
    me = next((m for m in monitors() if m.get("name") == name), None)
    pr = primary_for(name)
    if not me or not pr:
        return fallback
    px, py = int(pr.get("x", 0)), int(pr.get("y", 0))
    pw, ph = logical_size(pr)
    ew, eh = logical_size(me)
    if side == "left":
        return f"{px - ew}x{py}"
    if side == "up":
        return f"{px}x{py - eh}"
    if side == "down":
        return f"{px}x{py + ph}"
    return f"{px + pw}x{py}"   # right (default)


def valid_scale(width, height, scale):
    """Nearest scale Hyprland accepts for a width x height mode.

    Hyprland needs both logical dimensions to be whole pixels (it works in
    1/120 steps); anything else pops an "invalid scale" error, gets replaced by
    a guess, and our pre-computed position then overlaps the primary
    ("Monitor EXT-1 overlaps ..."). 2772x1280 @ 1.5 -> 853.3 px tall, so 4/3.
    """
    want = round(scale * 120)
    for d in range(0, 120):
        for k in (want - d, want + d):
            if k > 0 and (width * 120) % k == 0 and (height * 120) % k == 0:
                return round(k / 120, 6)
    return scale


def estimate_position(name, side, width, height, scale):
    """Pre-creation guess of the EXT position (before its snapped size exists)."""
    pr = primary_for(name)
    if not pr:
        return f"auto-{side}"
    px, py = int(pr.get("x", 0)), int(pr.get("y", 0))
    pw, ph = logical_size(pr)
    ew, eh = int(width / (scale or 1)), int(height / (scale or 1))
    return {"left": f"{px - ew}x{py}", "up": f"{px}x{py - eh}",
            "down": f"{px}x{py + ph}"}.get(side, f"{px + pw}x{py}")


def set_workspace_rules(name, primary_name):
    """Pin laptop workspaces to the primary monitor and EXT_WS to the phone."""
    for i in LAPTOP_WS:
        hyprctl("eval", f'hl.workspace_rule({{ workspace = "{i}", '
                        f'monitor = "{primary_name}" }})')
    for n, i in enumerate(EXT_WS):
        default = ", default = true" if n == 0 else ""
        hyprctl("eval", f'hl.workspace_rule({{ workspace = "{i}", '
                        f'monitor = "{name}"{default} }})')
    log(f"workspaces {LAPTOP_WS.start}-{LAPTOP_WS.stop - 1} -> {primary_name}, "
        f"{EXT_WS.start}-{EXT_WS.stop - 1} -> {name}")


def apply_live(name, width, height, fps, scale, side):
    """Re-apply mode/scale/side to an existing output (no stream restart)."""
    mode = f"{width}x{height}@{fps}"
    scale = valid_scale(width, height, scale)
    est = estimate_position(name, side, width, height, scale)
    ok = set_monitor(name, mode, est, scale)
    set_monitor(name, mode, flush_position(name, side, est), scale)
    return ok


def map_touch_device(device, output):
    """Confine an absolute touch device to one monitor (instead of the whole
    desktop layout). Re-applied on live changes: a config reload drops it."""
    r = hyprctl("eval", f'hl.device({{ name = "{device}", output = "{output}" }})')
    out = (r.stdout or "").lower()
    if r.returncode == 0 and "ok" in out and "error" not in out:
        return True
    return hyprctl("keyword", f"device[{device}]:output", output).returncode == 0


def remove_output(name):
    """Safely take the virtual output away, in an order Hyprland tolerates.

    1. stop anything still capturing it (a pending screencopy frame on a
       destroyed output can freeze the compositor),
    2. move focus / the cursor to the primary (removing the focused monitor is
       a crash path),
    3. move its workspaces (and windows) to the primary,
    4. remove it and hand the primary back to auto placement.
    """
    xdph.close_leftover_session()
    kill_stale_capture(name)
    if not any(m.get("name") == name for m in monitors()):
        return
    primary = primary_for(name)
    if primary:
        hyprctl("eval", f'hl.dispatch(hl.dsp.focus({{ '
                        f'monitor = "{primary["name"]}" }}))')
        try:
            for w in json.loads(hyprctl("workspaces", "-j").stdout):
                if w.get("monitor") == name:
                    hyprctl("eval",
                            f'hl.dispatch(hl.dsp.workspace.move({{ '
                            f'workspace = "{w["id"]}", '
                            f'monitor = "{primary["name"]}" }}))')
        except Exception as e:
            log(f"warning: could not reflow windows ({e})")
        time.sleep(0.1)             # let the moves render before the removal
    hyprctl("output", "remove", name)
    restore_primary(name)
    log(f"removed headless output {name}")


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


VA_DIRS = ("/usr/lib/dri", "/usr/lib64/dri", "/usr/lib/x86_64-linux-gnu/dri",
           "/usr/lib/aarch64-linux-gnu/dri")


def va_driver_for(node):
    """LIBVA_DRIVER_NAME that can *encode* on this render node, or None."""
    name = VA_DRIVERS.get(gpu_driver(node) or "")
    if name == "iHD" and not va_installed("iHD") and va_installed("i965"):
        name = "i965"                     # pre-Broadwell Intel / old distros
    return name


def va_installed(name):
    return any(os.path.exists(f"{d}/{name}_drv_video.so") for d in VA_DIRS)


def encoder_candidates(requested, device):
    """Encoders to try, best first: [(ffmpeg encoder, VA node, env, description)].

    auto: the hardware encoder of the GPU Hyprland renders on (zero-copy), then
    libx264 on the CPU, which works on any machine (NVIDIA without NVENC,
    nouveau, VMs, a VA driver without H.264 encode, ...).
    """
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
        env = {}
        va = va_driver_for(device)
        if va:
            # NVIDIA Hyprland setups often export LIBVA_DRIVER_NAME=nvidia
            # globally; that loads the decode-only driver on an AMD/Intel node.
            env["LIBVA_DRIVER_NAME"] = va
        out.append((requested, device, env,
                    f"VA-API on {device} ({va or 'default driver'})"))
    if requested != "libx264":
        out.append(("libx264", None, {}, "x264 on the CPU (fallback)"))
    else:
        out.append(("libx264", None, {}, "x264 on the CPU"))
    return out


XDPH_NVENC = "xdph+nvh264enc"   # pseudo-encoder: the xdph.py pipeline


def pick_encoder(requested, device):
    """The first choice of encoder_candidates (for display)."""
    return encoder_candidates(requested, device)[0]


def encoder_params(encoder, a):
    """wf-recorder -p options for minimum latency on each encoder."""
    common = [f"g={a.gop}", "bf=0", f"b={a.bitrate}"]
    if encoder.endswith("_nvenc"):
        return common + ["preset=p1", "tune=ull", "zerolatency=1",
                         "rc=cbr", "delay=0", "forced-idr=1"]
    if encoder.endswith("_vaapi"):
        return common + ["rc_mode=CBR", "async_depth=1"]
    # libx264: one slice-threaded frame in, one out
    return common + ["preset=ultrafast", "tune=zerolatency",
                     f"maxrate={a.bitrate}", f"bufsize={a.bitrate}"]


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


class HyprlandBackend:
    name = "hyprland"

    def __init__(self, args):
        self.a = args
        self.proc = None
        self.stream = None          # readable Annex-B byte stream
        self.session = None         # xdph.ScreenCastSession while capturing

    @staticmethod
    def missing():
        return [t for t in ("hyprctl", "wf-recorder") if not shutil.which(t)]

    def setup(self):
        """Create the headless output (or reuse one) and place it on `side`."""
        a = self.a
        kill_stale_capture(a.output)
        if any(m.get("name") == a.output for m in monitors()):
            log(f"reusing existing headless output {a.output}")
        else:
            primary = primary_for(a.output)
            if primary:
                set_workspace_rules(a.output, primary["name"])   # before create
                pin_primary(primary)                             # stop drift
            log(f"creating headless output {a.output}")
            hyprctl("output", "create", "headless", a.output)
            time.sleep(0.5)
        mode = f"{a.width}x{a.height}@{a.fps}"
        scale = valid_scale(a.width, a.height, a.scale)
        if scale != a.scale:
            log(f"scale {a.scale} gives fractional pixels at {a.width}x"
                f"{a.height}; using {scale:g}")
        est = estimate_position(a.output, a.side, a.width, a.height, scale)
        if not set_monitor(a.output, mode, est, scale):
            log(f"warning: could not apply mode {mode}")
        # Reposition flush now that the (possibly snapped) logical size is set.
        pos = flush_position(a.output, a.side, est)
        set_monitor(a.output, mode, pos, scale)
        log(f"output {a.output} ready ({mode}, scale {scale:g}, pos {pos})")

    def start_capture(self):
        """Launch wf-recorder; returns the readable Annex-B byte stream.

        Tries each encoder candidate in turn: one that can't initialise either
        exits within a moment (wrong GPU, missing driver/NVENC) or hangs without
        output (some Intel VA-API drivers), so we move on to the next instead of
        leaving the phone with a black screen.
        """
        a = self.a
        req = (a.encoder if a.encoder.startswith(("h264_", "hevc_", "libx26"))
               else "auto")
        cands = encoder_candidates(req, a.device)
        if a.codec == "hevc":
            cands = [c for c in cands if c[0] != XDPH_NVENC]   # H.264 only
        for i, (encoder, device, env, desc) in enumerate(cands):
            if a.codec == "hevc":
                encoder = encoder.replace("h264_", "hevc_").replace("264", "265")
            log(f"encoder: {desc}")
            try:
                if encoder == XDPH_NVENC:
                    self._spawn_xdph()
                else:
                    self._spawn(encoder, device, env)
            except Exception as e:
                log(f"encoder {encoder} could not start ({e}); trying the next one")
                self.stop_capture()
                continue
            if i == len(cands) - 1 or not self._fails_to_start():
                return self.stream
            self.stop_capture()
            log(f"encoder {encoder} failed to start; trying the next one")
        return self.stream

    def _spawn_xdph(self):
        """Zero-copy NVIDIA capture: an xdph screencast into GStreamer NVENC."""
        a = self.a
        self.session = xdph.ScreenCastSession(a.output)
        rfd, wfd = os.pipe()
        cmd = xdph.build_pipeline(self.session.node,
                                  gnome.bitrate_kbps(a.bitrate), a.gop, wfd)
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
        cmd += ["-r", str(a.fps),
                f"--muxer={'hevc' if a.codec == 'hevc' else 'h264'}",
                "-f", "/dev/stdout"]
        log("capture cmd: " + " ".join(cmd))
        # stdin=DEVNULL: wf-recorder otherwise prompts "/dev/stdout exists.
        # Overwrite? Y/n" and blocks forever (or eats the TUI's keystrokes).
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     env={**os.environ, **env},
                                     stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, bufsize=0,
                                     preexec_fn=_die_with_parent)
        self.stream = self.proc.stdout
        threading.Thread(target=self._wf_log, args=(self.proc,),
                         daemon=True).start()

    def _fails_to_start(self, grace=4.0):
        """True if wf-recorder exits, or produces nothing, within `grace` s.

        A working encoder emits its first access unit in ~0.1 s; one still
        silent after seconds is stuck (seen with VA-API on Intel) and would
        leave the phone on a black screen.
        """
        p = self.proc
        end = time.time() + grace
        while time.time() < end:
            if p.poll() is not None:
                return True
            r, _, _ = select.select([self.stream], [], [], 0.1)
            if r:      # bytes waiting (or EOF): alive a moment later = works
                time.sleep(0.2)
                return p.poll() is not None
        log(f"no output after {grace:g}s")
        return True

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
        if not map_touch_device(uinput_touch.DEVICE_NAME, self.a.output):
            log("warning: could not map the touch device to "
                f"{self.a.output}; touches may land on the wrong monitor")
        screen.description += f" mapped to {self.a.output}"
        return screen

    def apply_live(self, side, scale):
        a = self.a
        map_touch_device(uinput_touch.DEVICE_NAME, a.output)
        ok = apply_live(a.output, a.width, a.height, a.fps, scale, side)
        log(f"moved {a.output} side={side} scale={scale}")
        return ok

    def stop_capture(self):
        """Stop the encoder, then end the screencast (before any removal)."""
        p = self.proc
        if p and p.poll() is None:
            p.terminate()
            try:
                p.wait(timeout=3)
            except subprocess.TimeoutExpired:
                p.kill()
                p.wait()
        if self.stream and self.stream is not getattr(p, "stdout", None):
            self.stream.close()     # our end of the GStreamer pipe
        self.stream = None
        if self.session:
            self.session.close()
            self.session = None

    def teardown(self):
        remove_output(self.a.output)
