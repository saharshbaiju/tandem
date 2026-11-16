"""
gstreamer.py - PipeWire -> H.264 through gst-launch-1.0, for any backend.

Every backend that gets its frames as a PipeWire node (GNOME's virtual
monitor, the KWin virtual output, Hyprland's portal path on NVIDIA)
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

Every encoder setting is checked against what the installed element accepts
before gst-launch sees it (setting_value): a VA driver lists only the rate
control modes it can really do, and one value it doesn't list makes
gst-launch reject the whole pipeline ("could not set property"). A number
outside the element's range is clamped; a mode it doesn't offer makes the
encoder EncoderUnusable. EncoderSelector then starts the first encoder that
works, so a GPU whose hardware encoder can't be used still streams with the
next one (in the end x264 or OpenH264 on the CPU) instead of stopping.
"""

import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time

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


def _property_block(element, prop):
    """The gst-inspect lines describing `prop` of `element`: its header line
    and the indented lines under it (flags, type, range or enum values).
    Empty when the element or the property is unknown.

        rate-control        : The desired rate control mode for the encoder
                              flags: readable, writable
                              Enum "GstVaEncoderRateControl" Default: 2, "cbr"
                                 (2): cbr              - Constant Bitrate
                                 (16): cqp             - Constant Quantizer
    """
    lines = gst_inspect(element).splitlines()
    for i, line in enumerate(lines):
        if not line.strip().startswith((prop + " ", prop + ":")):
            continue
        header_indent = len(line) - len(line.lstrip())
        block = [line]
        for following in lines[i + 1:]:
            indent = len(following) - len(following.lstrip())
            if not following.strip() or indent <= header_indent:
                break
            block.append(following)
        return block
    return []


ENUM_VALUE = re.compile(r"^\s*\((-?\d+)\):\s*(\S+)")
INTEGER_RANGE = re.compile(r"Integer(?:64)?\.\s*Range:\s*(-?\d+)\s*-\s*(-?\d+)")


def enum_values(element, prop):
    """{nick: number} of an enum property, or None when `prop` is not an enum
    (or gst-inspect didn't describe it)."""
    values = {}
    for line in _property_block(element, prop)[1:]:
        match = ENUM_VALUE.match(line)
        if match:
            values[match.group(2)] = match.group(1)
    return values or None


def integer_range(element, prop):
    """(minimum, maximum) of an integer property, or None if it isn't one."""
    for line in _property_block(element, prop):
        match = INTEGER_RANGE.search(line)
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def setting_value(element, prop, value):
    """`value` as `element` will accept it for `prop`, or None if it won't.

    Only what gst-inspect describes is checked; a property it says nothing
    about passes unchanged. An enum value must be one the element lists (by
    name or number): VA elements list just the modes the driver supports.
    An integer is clamped into the property's range.
    """
    allowed = enum_values(element, prop)
    if allowed is not None:
        text = str(value)
        return text if text in allowed or text in allowed.values() else None
    limits = integer_range(element, prop)
    if limits is not None:
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        low, high = limits
        return str(min(max(number, low), high))
    return str(value)


def pick_encoder(preferred=None):
    """The requested encoder element if it exists, else the first available
    one from ENCODERS; None when there is none."""
    if preferred and preferred != "auto":
        return preferred if gst_inspect(preferred) else None
    return next((e for e in ENCODERS if gst_inspect(e)), None)


def installed_encoders():
    """The ENCODERS this system has, in preference order (hardware first)."""
    return [encoder for encoder in ENCODERS if gst_inspect(encoder)]


def encoder_candidates(preferred=None):
    """Encoders to try, in order: `preferred` first when it is one Tandem
    can drive and is installed, then every other installed one. A request
    that can't be honoured is logged and treated as "auto"; the rest of the
    list stays as the fallback either way."""
    installed = installed_encoders()
    if preferred and preferred != "auto":
        if preferred in installed:
            installed.remove(preferred)
            installed.insert(0, preferred)
        elif preferred not in ENCODERS:
            log(f"encoder '{preferred}' isn't one Tandem can use here "
                f"({', '.join(ENCODERS)}); choosing automatically")
        else:
            log(f"encoder '{preferred}' isn't installed; choosing "
                "automatically")
    return installed


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


class EncoderUnusable(RuntimeError):
    """This encoder element can't be configured the way Tandem needs (for
    example a VA driver without bitrate control); try the next one."""


def _with_props(element, *required, **optional):
    """`element req... key=value...`, every value one the element accepts.

    Required settings ("prop=value") are always written; a value the element
    rejects (a mode it doesn't list) raises EncoderUnusable rather than
    leaving gst-launch to fail on it, and a number out of range is clamped.
    Optional settings are given with "_" for "-" (cpb_size -> cpb-size),
    kept only when the element has the property, and dropped when it
    rejects the value, so an older plugin without one still parses.
    """
    parts = [element]
    for setting in required:
        prop, _, value = setting.partition("=")
        accepted = setting_value(element, prop, value)
        if accepted is None:
            raise EncoderUnusable(f"{element} doesn't accept {prop}={value}")
        parts.append(f"{prop}={accepted}")
    for key, value in optional.items():
        prop = key.replace("_", "-")
        if has_prop(element, prop):
            accepted = setting_value(element, prop, value)
            if accepted is not None:
                parts.append(f"{prop}={accepted}")
    return " ".join(parts)


# Rate control modes that hold the stream near the bitrate the user chose,
# best first. CBR is the goal; VBR with the small CPB below stays close to
# it. A driver offering neither (Intel's low-power encoder without its HuC
# firmware offers only constant-quantizer) can't keep frames small enough
# for the link, so that encoder is skipped for the next one.
BITRATE_MODES = ("cbr", "vbr")


def _bitrate_mode(element):
    """The best BITRATE_MODES entry `element` offers. Raises EncoderUnusable
    when it lists rate control modes but none of these."""
    offered = enum_values(element, "rate-control")
    if offered is None:          # not described: assume the usual CBR
        return "cbr"
    for mode in BITRATE_MODES:
        if mode in offered:
            return mode
    raise EncoderUnusable(
        f"{element} offers no bitrate control (rate-control: "
        f"{', '.join(offered)}); on Intel this usually means the GPU's HuC "
        "firmware isn't loaded")


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
            enc, f"rate-control={_bitrate_mode(enc)}", f"bitrate={kbps}",
            f"key-int-max={gop}", "b-frames=0", "target-usage=7",
            cpb_size=vbv_kbits(kbps, fps))
    if enc == "vaapih264enc":
        return (f"{videoconvert()} ! video/x-raw,format=NV12 ! " + _with_props(
            enc, f"rate-control={_bitrate_mode(enc)}", f"bitrate={kbps}",
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
    ("GNOME", "KDE")."""
    return _encoders_for(args, backend_label)[0]


def _encoders_for(args, backend_label):
    """encoder_candidates() for tandemd's `args`, after the h264-only note.
    Raises RuntimeError when none is installed."""
    if args.codec != "h264":
        log(f"note: {backend_label} backend encodes h264 only; ignoring "
            f"codec '{args.codec}'")
    candidates = encoder_candidates(getattr(args, "encoder", None))
    if not candidates:
        raise RuntimeError("no usable GStreamer H.264 encoder found")
    return candidates


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


# An encoder that exits with an error this soon after it was started is
# treated as one that can't run here: gst-launch rejecting a setting, or a
# driver refusing the session, ends it within a second. tandemd's
# supervise_capture restarts capture when the encoder exits, and that restart
# moves on to the next encoder instead of retrying this one until tandemd
# gives up. Starting never waits for the encoder: the listener only opens
# once start_capture returns, and an idle compositor may send no frame at all.
QUICK_EXIT = 5.0


def ended_with_an_error(proc):
    """True if gst-launch `proc` has exited and it wasn't a clean exit or our
    own stop_pipeline (SIGTERM, then SIGKILL)."""
    code = proc.poll()
    return code is not None and code not in (0, -signal.SIGTERM,
                                             -signal.SIGKILL)


class EncoderSelector:
    """Starts the first encoder that can run on a PipeWire node and
    remembers it, so capture restarts within a session go straight back to it.

    One per backend instance; GNOME and KDE call start() from their
    start_capture(). An encoder fails in one of two ways, and either way it
    is logged and skipped for the next one, so a broken hardware encoder ends
    with a CPU encoder rather than no stream:

    * a setting it doesn't accept (EncoderUnusable): found before it starts,
      and the next one is started at once;
    * it exits with an error within QUICK_EXIT: tandemd sees the stream end
      and restarts capture, and that start() moves on to the next one.
    """

    def __init__(self, args, backend_label):
        self.args = args
        self.backend_label = backend_label
        self.working = None       # the encoder that started last
        self.failed = set()       # encoders that failed in this session
        self._last = None         # (proc, start time) of the last start

    def _note_how_the_last_one_ended(self):
        """Mark the last started encoder failed if it ended with an error
        soon after starting. Stopped by us (stop_capture) is not a failure."""
        if self._last is None:
            return
        proc, started = self._last
        self._last = None
        if (ended_with_an_error(proc)
                and time.monotonic() - started < QUICK_EXIT
                and self.working is not None):
            log(f"encoder {self.working} failed right after starting (exit "
                f"code {proc.returncode}); trying the next one")
            self.failed.add(self.working)
            self.working = None

    def candidates(self):
        """The order to try encoders in this time: the one that worked
        first, the ones that failed in this session last (they are retried
        only when nothing else is left)."""
        ordered = _encoders_for(self.args, self.backend_label)
        if self.working in ordered:
            ordered.remove(self.working)
            ordered.insert(0, self.working)
        return ([e for e in ordered if e not in self.failed]
                + [e for e in ordered if e in self.failed])

    def start(self, node):
        """(proc, stream) of the first encoder that works on `node`.
        Raises RuntimeError naming every encoder tried when none does."""
        self._note_how_the_last_one_ended()
        tried = []
        for encoder in self.candidates():
            tried.append(encoder)
            try:
                proc, stream = start_encoder(node, self.args, encoder)
            except EncoderUnusable as e:
                log(f"encoder {encoder} can't be used: {e}; trying the next one")
                self.failed.add(encoder)
                continue
            if encoder != tried[0]:
                log(f"using encoder {encoder}")
            self.working = encoder
            self._last = (proc, time.monotonic())
            return proc, stream
        raise RuntimeError("no H.264 encoder can be used on this system "
                           f"(tried: {', '.join(tried)}); see the encoder "
                           "messages above")


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
