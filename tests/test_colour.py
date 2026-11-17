"""Tests for the BT.709 colour convention (colour.py, GitHub issue #7): the
wf-recorder arguments per encoder, what happens on an ffmpeg that lacks the
options, and Hyprland retrying an encoder without them.

ffmpeg is never run: the filter options it would report are faked.
"""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import types
import unittest
from unittest import mock

import colour
from backends import gstreamer, hyprland

ALL = frozenset({"out_color_matrix", "out_range", "format"})
TAGS = ["-p", "colorspace=bt709", "-p", "color_primaries=bt709",
        "-p", "color_trc=bt709", "-p", "color_range=tv"]


def with_filters(options):
    """Patch ffmpeg_filter_options to answer from `options` (name -> set)."""
    return mock.patch.object(colour, "ffmpeg_filter_options",
                             lambda name: options.get(name, frozenset()))


class WfRecorderArgsTest(unittest.TestCase):
    def test_cpu_encoders_convert_with_swscale_and_label(self):
        with with_filters({"scale": ALL}):
            for encoder in ("libx264", "libopenh264"):
                self.assertEqual(colour.wf_recorder_args(encoder), [
                    "-F", "scale=out_color_matrix=bt709:out_range=tv,"
                          "format=yuv420p"] + TAGS)

    def test_vaapi_converts_on_the_gpu(self):
        with with_filters({"scale_vaapi": ALL}):
            self.assertEqual(colour.wf_recorder_args("h264_vaapi"), [
                "-F", "scale_vaapi=format=nv12:out_color_matrix=bt709:"
                      "out_range=tv"] + TAGS)

    def test_nvenc_converts_itself_from_the_labels(self):
        with with_filters({}):
            self.assertEqual(colour.wf_recorder_args("h264_nvenc"), TAGS)

    def test_old_scale_vaapi_is_left_alone_not_mislabelled(self):
        with with_filters({"scale_vaapi": frozenset({"format"})}):
            self.assertEqual(colour.wf_recorder_args("h264_vaapi"), [])

    def test_without_an_ffmpeg_binary_swscale_is_assumed(self):
        with with_filters({}):
            self.assertEqual(colour.wf_recorder_args("libx264")[:2], [
                "-F", "scale=out_color_matrix=bt709:out_range=tv,"
                      "format=yuv420p"])

    def test_gst_caps(self):
        self.assertEqual(colour.gst_caps("video/x-raw,format=NV12"),
                         "video/x-raw,format=NV12,colorimetry=bt709")


class GstChainTest(unittest.TestCase):
    def test_unpinned_chain_is_the_one_before_0_9(self):
        with mock.patch.object(gstreamer, "gst_inspect", return_value=""):
            pinned = gstreamer.encoder_chain("x264enc", 8000, 30)
            plain = gstreamer.encoder_chain("x264enc", 8000, 30,
                                            pin_colour=False)
        self.assertIn("format=I420,colorimetry=bt709 !", pinned)
        self.assertNotIn("colorimetry", plain)
        self.assertEqual(pinned.replace(",colorimetry=bt709", ""), plain)

    def test_va_memory_caps_are_pinned_too(self):
        with mock.patch.object(gstreamer, "gst_inspect",
                               side_effect=lambda e: "x" if e == "vapostproc"
                               else ""):
            chain = gstreamer.encoder_chain("vah264enc", 8000, 30)
        self.assertIn("vapostproc ! video/x-raw(memory:VAMemory),format=NV12,"
                      "colorimetry=bt709 !", chain)


class HyprlandRetryTest(unittest.TestCase):
    """start_capture: an encoder that fails with the colour settings is
    started again without them before the next encoder is tried."""

    def make(self, fails):
        """A backend whose _spawn records calls; `fails(encoder, plain)` says
        whether that start fails."""
        backend = hyprland.HyprlandBackend.__new__(hyprland.HyprlandBackend)
        backend.a = types.SimpleNamespace(codec="h264")
        backend._working = None
        backend._working_plain = False
        backend.stream = "stream"
        backend.spawned = []
        backend.stop_capture = lambda: None
        backend._candidates = lambda: [("h264_vaapi", "/dev/dri/renderD128",
                                        {}, "VA-API"),
                                       ("libx264", None, {}, "x264")]
        state = {}

        def spawn(encoder, device, env, plain_colour=False):
            backend.spawned.append((encoder, plain_colour))
            state["last"] = (encoder, plain_colour)

        backend._spawn = spawn
        backend._fails_to_start = lambda: ("exited" if fails(*state["last"])
                                           else None)
        return backend

    def test_colour_options_rejected_keeps_the_same_encoder(self):
        backend = self.make(lambda encoder, plain: not plain)
        with mock.patch.object(hyprland, "log"):
            backend.start_capture()
        self.assertEqual(backend.spawned, [("h264_vaapi", False),
                                           ("h264_vaapi", True)])
        self.assertTrue(backend._working_plain)
        # a restart goes straight to what worked, without the colour options
        backend.spawned.clear()
        with mock.patch.object(hyprland, "log"):
            backend.start_capture()
        self.assertEqual(backend.spawned, [("h264_vaapi", True)])

    def test_a_broken_encoder_still_falls_through(self):
        backend = self.make(lambda encoder, plain: encoder == "h264_vaapi")
        with mock.patch.object(hyprland, "log"):
            backend.start_capture()
        self.assertEqual(backend.spawned, [("h264_vaapi", False),
                                           ("h264_vaapi", True),
                                           ("libx264", False)])
        self.assertFalse(backend._working_plain)


if __name__ == "__main__":
    unittest.main()
