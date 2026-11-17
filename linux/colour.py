"""
colour.py - the one colour convention every Tandem stream uses.

The desktop is RGB; H.264 carries YUV. Turning one into the other needs a
matrix (BT.601 or BT.709) and a range (full 0-255 or limited 16-235), and the
phone has to undo it with the *same* two. Nothing agrees on a default:

    ffmpeg / swscale         BT.601 matrix
    wf-recorder              labels its stream full range, matrix unset
    VA-API drivers (Mesa)    BT.601 unless told otherwise
    openh264                 writes no colour information at all
    Android decoders         guess for unlabelled video, usually BT.709
                             limited for HD, and it varies by vendor

so before 0.9 the colours depended on the GPU, the encoder and the phone:
washed out or slightly off (GitHub issue #7).

Tandem therefore fixes the convention at every step:

  1. convert RGB -> YUV as BT.709, limited range (what HD video uses, and
     what game-streaming and recording tools send),
  2. label the H.264 stream with it (VUI: matrix, range, primaries,
     transfer), so any decoder knows what it got,
  3. and the app tells its decoder the same thing, so a stream without a
     label (openh264) never depends on the phone's guess.

Options are only used where the installed ffmpeg / GStreamer has them
(checked once): an old install keeps streaming, with the old colours, rather
than losing its encoder.
"""

import functools
import shutil
import subprocess

# GStreamer: BT.709 matrix, primaries and transfer, limited range.
GST_COLORIMETRY = "bt709"

# ffmpeg codec options (wf-recorder -p): label the stream. NVENC also picks
# its own RGB -> YUV matrix and range from these when it gets RGB frames.
FFMPEG_CODEC_PARAMS = ("colorspace=bt709", "color_primaries=bt709",
                       "color_trc=bt709", "color_range=tv")


def gst_caps(caps):
    """`caps` (a raw-video caps string) pinned to Tandem's colorimetry."""
    return f"{caps},colorimetry={GST_COLORIMETRY}"


@functools.lru_cache(maxsize=None)
def ffmpeg_filter_options(name):
    """Option names of ffmpeg filter `name` (from `ffmpeg -h filter=`), or
    an empty set when ffmpeg isn't installed or doesn't know the filter.
    wf-recorder links the same libavfilter as the ffmpeg binary."""
    if not shutil.which("ffmpeg"):
        return frozenset()
    try:
        out = subprocess.run(["ffmpeg", "-hide_banner", "-h", f"filter={name}"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    return frozenset(word for line in out.splitlines()
                     if (word := line.strip().split(" ")[0]).isidentifier())


def wf_recorder_filter(encoder):
    """The wf-recorder -F filter that converts to BT.709 limited for
    `encoder`, or None to leave wf-recorder's own conversion alone.

    VA-API: the conversion runs on the GPU (scale_vaapi), told the matrix
    and range. NVENC takes RGB and converts by itself from the codec
    options, so it needs no filter. CPU encoders: swscale, then 4:2:0.
    """
    if encoder.endswith("_nvenc"):
        return None
    if encoder.endswith("_vaapi"):
        options = ffmpeg_filter_options("scale_vaapi")
        if {"out_color_matrix", "out_range"} <= options:
            return "scale_vaapi=format=nv12:out_color_matrix=bt709:out_range=tv"
        return None
    options = ffmpeg_filter_options("scale")
    if options and not {"out_color_matrix", "out_range"} <= options:
        return None
    # No ffmpeg binary to ask: swscale has had both options for a decade.
    return "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"


def wf_recorder_args(encoder):
    """wf-recorder arguments that make `encoder` follow the convention."""
    convert = wf_recorder_filter(encoder)
    if convert is None and not encoder.endswith("_nvenc"):
        return []        # can't control the conversion: don't mislabel it
    args = ["-F", convert] if convert else []
    for param in FFMPEG_CODEC_PARAMS:
        args += ["-p", param]
    return args
