"""Tests for GnomeBackend.start_capture / stop_capture on top of the shared
gstreamer.spawn_pipeline / stop_pipeline (no Mutter needed: the encoder
process is mocked)."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import io
import subprocess
import types
import unittest
from unittest import mock

from backends import gnome, gstreamer

EXPECTED_DESCRIPTION = (
    "gst-launch-1.0 -q pipewiresrc path=77 do-timestamp=true "
    "keepalive-time=1000 ! video/x-raw,width=2400,height=1080 "
    "! queue max-size-buffers=1 max-size-bytes=0 max-size-time=0 "
    "leaky=downstream ! videoconvert n-threads=4 ! video/x-raw,format=I420 "
    "! x264enc tune=zerolatency speed-preset=ultrafast bitrate=12000 "
    "key-int-max=30 bframes=0 threads=2 sliced-threads=false "
    "byte-stream=true vbv-buf-capacity=33 ! h264parse config-interval=-1 "
    "! video/x-h264,stream-format=byte-stream,alignment=au "
    "! fdsink fd={fd} sync=false")


def fake_encoder_process():
    proc = mock.Mock()
    proc.stderr = io.BytesIO()
    proc.poll.return_value = None
    proc.wait.return_value = 0
    return proc


class GnomeCaptureTest(unittest.TestCase):
    def setUp(self):
        saved = dict(gstreamer._inspect_cache)
        self.addCleanup(lambda: (gstreamer._inspect_cache.clear(),
                                 gstreamer._inspect_cache.update(saved)))
        gstreamer._inspect_cache.clear()
        gstreamer._inspect_cache.update({
            "x264enc": "  vbv-buf-capacity    : x\n",
            "pipewiresrc": "  keepalive-time      : x\n",
            "videoconvert": "  n-threads           : x\n"})
        for missing in ("vah264enc", "vah264lpenc", "vaapih264enc",
                        "openh264enc", "vapostproc"):
            gstreamer._inspect_cache[missing] = ""
        for patcher in (mock.patch("os.cpu_count", return_value=8),
                        mock.patch.object(gnome.GnomeBackend,
                                          "_place_when_ready")):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.args = types.SimpleNamespace(
            codec="h264", encoder="auto", width=2400, height=1080,
            bitrate="12M", gop=30, fps=60, side="right", scale=1.0)
        self.backend = gnome.GnomeBackend(self.args)
        self.backend.node = 77

    def start(self):
        with mock.patch.object(gstreamer.subprocess, "Popen",
                               side_effect=lambda *a, **k: fake_encoder_process()
                               ) as popen, \
                mock.patch.object(gstreamer, "log") as log:
            stream = self.backend.start_capture()
        self.addCleanup(stream.close)
        return popen, log, stream

    def test_start_capture_runs_the_same_pipeline_as_before(self):
        popen, log, stream = self.start()
        (argv,), kwargs = popen.call_args
        write_fd = kwargs["pass_fds"][0]
        self.assertEqual(" ".join(argv), EXPECTED_DESCRIPTION.format(fd=write_fd))
        self.assertEqual(kwargs, {"stdin": subprocess.DEVNULL,
                                  "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.PIPE,
                                  "pass_fds": (write_fd,),
                                  "start_new_session": True})
        log.assert_called_once_with(
            "encoder x264enc; capture cmd: "
            + EXPECTED_DESCRIPTION.format(fd=write_fd))
        self.assertEqual((stream.mode, stream.closed), ("rb", False))
        self.backend._place_when_ready.assert_called_once()

    def test_start_capture_again_stops_the_previous_encoder(self):
        self.start()
        first = self.backend.proc
        self.start()
        first.terminate.assert_called_once()
        self.assertIsNot(self.backend.proc, first)

    def test_stop_capture_is_idempotent(self):
        self.start()
        proc = self.backend.proc
        self.backend.stop_capture()
        self.backend.stop_capture()
        proc.terminate.assert_called_once()
        self.assertIsNone(self.backend.proc)

    def test_no_encoder_is_a_clear_error(self):
        for name in gstreamer.ENCODERS:
            gstreamer._inspect_cache[name] = ""
        with self.assertRaisesRegex(RuntimeError, "no usable GStreamer H.264"):
            self.backend.start_capture()


if __name__ == "__main__":
    unittest.main()
