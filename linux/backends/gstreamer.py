"""
gstreamer.py - PipeWire -> H.264 through gst-launch-1.0, for any backend.

Every backend that gets its frames as a PipeWire node (GNOME's virtual
monitor, the KWin and niri virtual outputs, Hyprland's portal path on NVIDIA)
encodes them the same way:

    gst-launch-1.0 pipewiresrc path=<node> ! video/x-raw,width=W,height=H
        ! 1-frame leaky queue ! <H.264 encoder> ! h264parse
        ! byte-stream AU ! fdsink fd=<write end of a pipe>
        -> raw Annex-B on the pipe's read end -> tandemd's parser

This module holds that shared part: what a backend's missing() reports for
GStreamer (missing_pipeline_tools), probing which elements and properties
exist, picking an encoder (choose_encoder), building the pipeline
description, and starting gst-launch on a node (start_encoder) with its
stderr forwarded to our log. The compositor side (creating the output,
finding its node, placing it) stays in each backend.

Encoder rules every chain follows: one slice per frame and no B-frames (the
phone's hardware decoder wants exactly one complete access unit per input
buffer), the fastest preset with no lookahead, and a rate-control buffer of
VBV_FRAMES frames so a keyframe can't grow big enough to stall the Wi-Fi link.
"""

import os
import shutil
import subprocess
import sys
import threading

# Hardware encoders first; the VA plugins only register an element when the
# driver can really encode H.264, so "element exists" is a usable probe.
ENCODERS = ("vah264enc", "vah264lpenc", "vaapih264enc", "x264enc", "openh264enc")
# The command-line tools and the elements every pipeline built here uses.
GST_TOOLS = ("gst-launch-1.0", "gst-inspect-1.0")
PIPELINE_ELEMENTS = ("pipewiresrc", "h264parse", "videoconvert")


def log(*a):
    # Never raise: once the terminal or the TUI reading our stderr is gone,
    # a print would throw, and in the gst log thread that would stop the
    # draining of gst-launch's stderr until its pipe fills and it stalls.
    try:
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


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
    """The requested encoder element if it exists, else the first available
    one from ENCODERS; None when there is none."""
    if preferred and preferred != "auto":
        return preferred if gst_inspect(preferred) else None
    return next((e for e in ENCODERS if gst_inspect(e)), None)


def missing_pipeline_tools():
    """What this system lacks to run the pipeline: "gst-launch-1.0" /
    "gst-inspect-1.0", "gstreamer:<element>" for a missing element, and
    "gstreamer:h264-encoder" when no encoder in ENCODERS exists. Elements
    can only be checked once gst-inspect-1.0 is there. Empty if nothing.

    The part of every PipeWire backend's missing() that is the same for
    all of them; each adds its own needs (PyGObject, kscreen-doctor...).
    """
    missing = [tool for tool in GST_TOOLS if not shutil.which(tool)]
    if shutil.which("gst-inspect-1.0"):
        missing += [f"gstreamer:{element}" for element in PIPELINE_ELEMENTS
                    if not gst_inspect(element)]
        if not pick_encoder():
            missing.append("gstreamer:h264-encoder")
    return missing


def bitrate_kbps(bitrate):
    """'12M' / '800K' / '12000000' -> kbit/s."""
    s = str(bitrate).strip().upper()
    if s.endswith("M"):
        return int(float(s[:-1]) * 1000)
    if s.endswith("K"):
        return int(float(s[:-1]))
    return max(1, int(float(s)) // 1000)


# --------------------------------------------------------------------------- #
# Pipeline description
# --------------------------------------------------------------------------- #

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
    VBV_FRAMES frames where the element exposes one. The encoder input is
    always pinned to a 4:2:0 format (NV12 / I420): left to negotiate, x264
    would pick 4:4:4 from an RGB source, which phones can't decode.
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
    """gst-launch argv: PipeWire node -> H.264 Annex-B access units on `fd`.

    The width/height caps decide the frame size the encoder sees. For
    Mutter they also size the virtual monitor itself (it takes the size its
    consumer asks for); for an output created at a fixed mode they match it.
    """
    desc = (f"{pipewiresrc(node)} ! video/x-raw,width={width},height={height} "
            f"! {LEAKY_QUEUE} "
            f"! {encoder_chain(enc, kbps, gop, fps)} "
            f"! h264parse config-interval=-1 "
            f"! video/x-h264,stream-format=byte-stream,alignment=au "
            f"! fdsink fd={fd} sync=false")
    return gst_launch(desc)


# --------------------------------------------------------------------------- #
# Running the pipeline
# --------------------------------------------------------------------------- #

def spawn_pipeline(build_description, label="gst"):
    """Start gst-launch writing H.264 into a new pipe; returns (proc, stream).

    `build_description(fd)` returns the gst-launch argv for a pipeline whose
    fdsink writes to `fd` (the write end of the pipe, which is passed to the
    child), for example
    `lambda fd: build_pipeline(node, w, h, kbps, gop, enc, fd=fd, fps=fps)`.
    It is the place to log the command line, since only it sees the fd.
    `stream` is the unbuffered read end that tandemd parses; the caller owns
    it and closes it. gst-launch's stderr is forwarded to our log, one line
    at a time, as "<label>: ...".

    The encoder runs in its own session: a Ctrl-C or hangup on the launching
    terminal must not kill it behind the daemon's back. It still dies with
    the daemon: its next write to the pipe gets EPIPE once our end is gone,
    and the compositor drops the PipeWire node with our session.
    Raises whatever starting the process raised (OSError: gst-launch-1.0 not
    installed), with no file descriptor left open.
    """
    read_fd, write_fd = os.pipe()
    try:
        cmd = build_description(write_fd)
        proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.PIPE, pass_fds=(write_fd,),
                                start_new_session=True)
    except BaseException:
        os.close(read_fd)
        raise
    finally:
        os.close(write_fd)          # our copy; EOF arrives when gst exits
    # The thread gets *this* process: after a capture restart the backend
    # holds the next one, and this thread must finish with the old one.
    threading.Thread(target=forward_stderr, args=(proc, label),
                     name=f"tandem-{label}-log", daemon=True).start()
    return proc, os.fdopen(read_fd, "rb", buffering=0)


def choose_encoder(args, backend_label):
    """The encoder element for tandemd's `args` (--encoder, "auto" for the
    best one available). Notes in the log that only H.264 is encoded when
    --codec asks for something else. Raises RuntimeError when there is no
    usable encoder. `backend_label` names the backend in that note
    ("GNOME", "KDE", "niri")."""
    if args.codec != "h264":
        log(f"note: {backend_label} backend encodes h264 only; ignoring "
            f"codec '{args.codec}'")
    encoder = pick_encoder(getattr(args, "encoder", None))
    if not encoder:
        raise RuntimeError("no usable GStreamer H.264 encoder found")
    return encoder


def start_encoder(node, args, encoder):
    """Encode PipeWire `node` with `encoder` at the size, bitrate, GOP and
    frame rate in tandemd's `args`; returns (proc, stream) as
    spawn_pipeline() does, after logging the command line.

    The caps pin the frames to args.width x args.height, the size the
    backend asked its compositor for. The caller stops any encoder it
    already runs on `node` first: one PipeWire node, one consumer.
    """
    fps = getattr(args, "fps", 60) or 60

    def pipeline_writing_to(fd):
        command = build_pipeline(node, args.width, args.height,
                                 bitrate_kbps(args.bitrate), args.gop,
                                 encoder, fd=fd, fps=fps)
        log(f"encoder {encoder}; capture cmd: " + " ".join(command))
        return command

    return spawn_pipeline(pipeline_writing_to)


def forward_stderr(proc, label="gst"):
    """Log each line `proc` writes to stderr until it exits (keeps its stderr
    pipe drained, so a chatty pipeline can never block on a full pipe), then
    close our end of that pipe so a restart doesn't leak one."""
    try:
        for line in proc.stderr:
            line = line.decode("utf-8", "replace").rstrip()
            if line:
                log(f"{label}: " + line)
    finally:
        proc.stderr.close()


def stop_pipeline(proc):
    """Stop a gst-launch process: SIGTERM, then SIGKILL after 3 s.

    Idempotent: None or an already exited process is left alone.
    """
    if proc and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
