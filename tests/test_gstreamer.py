"""Tests for backends/gstreamer.py: pipeline descriptions, encoder choice,
and running gst-launch through spawn_pipeline / stop_pipeline."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import io
import os
import subprocess
import time
import types
import unittest
from unittest import mock

from backends import gnome, gstreamer

# Every name gnome.py exported before the helpers moved out; xdph.py,
# hyprland.py, core.py and testsrc.py use them as gnome.<name>.
MOVED_NAMES = ("ENCODERS", "_inspect_cache", "gst_inspect", "has_prop",
               "pick_encoder", "bitrate_kbps", "VBV_FRAMES", "vbv_ms",
               "vbv_kbits", "_with_props", "videoconvert", "encoder_chain",
               "LEAKY_QUEUE", "pipewiresrc", "gst_launch", "build_pipeline")


def inspect_output(*props):
    """Fake `gst-inspect-1.0` text listing these properties."""
    lines = ["Factory Details:", "  Rank                     primary"]
    lines += [f"  {prop:<24}: a property" for prop in props]
    return "\n".join(lines) + "\n"


class FakeInspect:
    """Context manager: gst_inspect answers from `elements` (name -> props),
    every other element is missing."""

    def __init__(self, elements):
        self.elements = elements

    def __enter__(self):
        self.saved = dict(gstreamer._inspect_cache)
        gstreamer._inspect_cache.clear()
        self.patch = mock.patch.object(
            gstreamer.subprocess, "run",
            side_effect=lambda argv, **_kw: subprocess.CompletedProcess(
                argv, 0 if argv[1] in self.elements else 1,
                inspect_output(*self.elements.get(argv[1], ())), ""))
        self.patch.start()
        return self

    def __exit__(self, *exc):
        self.patch.stop()
        gstreamer._inspect_cache.clear()
        gstreamer._inspect_cache.update(self.saved)


class ReExportTest(unittest.TestCase):
    def test_gnome_reexports_the_same_objects(self):
        for name in MOVED_NAMES + ("spawn_pipeline", "stop_pipeline"):
            with self.subTest(name=name):
                self.assertIs(getattr(gnome, name), getattr(gstreamer, name))

    def test_gnome_and_gstreamer_share_one_inspect_cache(self):
        self.assertIs(gnome._inspect_cache, gstreamer._inspect_cache)


class ValuesTest(unittest.TestCase):
    def test_bitrate_kbps(self):
        cases = {"12M": 12000, "800K": 800, "12000000": 12000, " 4m ": 4000,
                 "1.5M": 1500, "999": 1, 20000000: 20000}
        for given, expected in cases.items():
            with self.subTest(given=given):
                self.assertEqual(gstreamer.bitrate_kbps(given), expected)

    def test_vbv_is_two_frames(self):
        self.assertEqual(gstreamer.vbv_ms(60), 33)
        self.assertEqual(gstreamer.vbv_ms(30), 67)
        self.assertEqual(gstreamer.vbv_ms(0), 2000)       # fps 0 -> 1
        self.assertEqual(gstreamer.vbv_kbits(12000, 60), 400)
        self.assertEqual(gstreamer.vbv_kbits(0, 60), 1)    # never 0

    def test_gst_launch_has_no_eos_flag(self):
        self.assertEqual(gstreamer.gst_launch("a ! b  c=1"),
                         ["gst-launch-1.0", "-q", "a", "!", "b", "c=1"])


class ProbingTest(unittest.TestCase):
    def test_has_prop_accepts_a_name_running_into_the_colon(self):
        text = ("  bitrate             : x\n"
                "  repeat-sequence-header: y\n")
        with mock.patch.object(gstreamer, "gst_inspect", return_value=text):
            self.assertTrue(gstreamer.has_prop("e", "bitrate"))
            self.assertTrue(gstreamer.has_prop("e", "repeat-sequence-header"))
            self.assertFalse(gstreamer.has_prop("e", "bit"))

    def test_gst_inspect_caches_and_reports_missing_as_empty(self):
        with FakeInspect({"x264enc": ()}):
            self.assertTrue(gstreamer.gst_inspect("x264enc"))
            self.assertEqual(gstreamer.gst_inspect("nope"), "")
            self.assertEqual(gstreamer.subprocess.run.call_count, 2)
            gstreamer.gst_inspect("x264enc")
            self.assertEqual(gstreamer.subprocess.run.call_count, 2)

    def test_gst_inspect_survives_a_missing_binary(self):
        saved = dict(gstreamer._inspect_cache)
        gstreamer._inspect_cache.clear()
        try:
            with mock.patch.object(gstreamer.subprocess, "run",
                                   side_effect=FileNotFoundError):
                self.assertEqual(gstreamer.gst_inspect("x264enc"), "")
        finally:
            gstreamer._inspect_cache.clear()
            gstreamer._inspect_cache.update(saved)

    def test_pick_encoder_prefers_hardware_and_honours_a_request(self):
        with FakeInspect({"x264enc": (), "openh264enc": (), "vah264enc": ()}):
            self.assertEqual(gstreamer.pick_encoder(), "vah264enc")
            self.assertEqual(gstreamer.pick_encoder("auto"), "vah264enc")
            self.assertEqual(gstreamer.pick_encoder("x264enc"), "x264enc")
            self.assertIsNone(gstreamer.pick_encoder("nvh264enc"))
        with FakeInspect({}):
            self.assertIsNone(gstreamer.pick_encoder())


class MissingPipelineToolsTest(unittest.TestCase):
    """The checks GNOME, KDE and niri share in their missing()."""

    def which(self, *present):
        return mock.patch.object(gstreamer.shutil, "which",
                                 side_effect=lambda tool: (
                                     f"/usr/bin/{tool}" if tool in present
                                     else None))

    def test_without_gstreamer_tools_elements_are_not_probed(self):
        with self.which(), FakeInspect({}) as inspect:
            self.assertEqual(gstreamer.missing_pipeline_tools(),
                             ["gst-launch-1.0", "gst-inspect-1.0"])
            inspect.patch.target.run.assert_not_called()

    def test_missing_elements_and_encoder(self):
        with self.which("gst-launch-1.0", "gst-inspect-1.0"), \
                FakeInspect({"videoconvert": ()}):
            self.assertEqual(gstreamer.missing_pipeline_tools(),
                             ["gstreamer:pipewiresrc", "gstreamer:h264parse",
                              "gstreamer:h264-encoder"])

    def test_nothing_missing(self):
        with self.which("gst-launch-1.0", "gst-inspect-1.0"), \
                FakeInspect({"pipewiresrc": (), "h264parse": (),
                             "videoconvert": (), "openh264enc": ()}):
            self.assertEqual(gstreamer.missing_pipeline_tools(), [])

    def test_every_pipewire_backend_reports_the_same_gaps(self):
        from backends import kde, niri
        shared = ["gstreamer:pipewiresrc", "gstreamer:h264-encoder"]
        with self.which("gst-launch-1.0", "gst-inspect-1.0",
                        "kscreen-doctor"), \
                FakeInspect({"videoconvert": (), "h264parse": ()}), \
                mock.patch.object(gnome, "Gio", object()), \
                mock.patch.object(niri, "Gio", object()), \
                mock.patch.object(kde.kwin_helper, "is_current",
                                  return_value=True):
            for backend in (gnome.GnomeBackend, kde.KdeBackend,
                            niri.NiriBackend):
                with self.subTest(backend=backend.name):
                    self.assertEqual(backend.missing(), shared)


class EncoderStartTest(unittest.TestCase):
    """choose_encoder() / start_encoder(): the start of every PipeWire
    backend's start_capture()."""

    def args(self, **values):
        defaults = dict(codec="h264", encoder="auto", width=2400,
                        height=1080, bitrate="12M", gop=30, fps=60)
        defaults.update(values)
        return types.SimpleNamespace(**defaults)

    def test_choose_encoder_notes_an_ignored_codec(self):
        with FakeInspect({"x264enc": ()}), \
                mock.patch.object(gstreamer, "log") as log:
            self.assertEqual(gstreamer.choose_encoder(
                self.args(codec="hevc"), "KDE"), "x264enc")
        log.assert_called_once_with(
            "note: KDE backend encodes h264 only; ignoring codec 'hevc'")

    def test_choose_encoder_without_one_is_a_clear_error(self):
        with FakeInspect({}), mock.patch.object(gstreamer, "log") as log:
            with self.assertRaisesRegex(RuntimeError,
                                        "no usable GStreamer H.264"):
                gstreamer.choose_encoder(self.args(), "niri")
        log.assert_not_called()

    def test_start_encoder_builds_logs_and_spawns(self):
        process, stream = mock.Mock(), mock.Mock()
        with mock.patch.object(gstreamer, "build_pipeline",
                               return_value=["gst-launch-1.0", "x"]) as build, \
                mock.patch.object(gstreamer, "spawn_pipeline",
                                  side_effect=lambda describe: (
                                      describe(7) and (process, stream))), \
                mock.patch.object(gstreamer, "log") as log:
            result = gstreamer.start_encoder(42, self.args(fps=None),
                                             "x264enc")
        self.assertEqual(result, (process, stream))
        build.assert_called_once_with(42, 2400, 1080, 12000, 30, "x264enc",
                                      fd=7, fps=60)
        log.assert_called_once_with(
            "encoder x264enc; capture cmd: gst-launch-1.0 x")


class PipelineTest(unittest.TestCase):
    ELEMENTS = {"x264enc": ("vbv-buf-capacity",), "openh264enc": (),
                "vah264enc": ("cpb-size",), "vaapih264enc": ("cpb-length",),
                "videoconvert": ("n-threads",),
                "pipewiresrc": ("keepalive-time",)}

    def setUp(self):
        self.inspect = FakeInspect(self.ELEMENTS)
        self.inspect.__enter__()
        self.addCleanup(self.inspect.__exit__)
        cpu_patch = mock.patch("os.cpu_count", return_value=8)
        cpu_patch.start()
        self.addCleanup(cpu_patch.stop)

    def test_x264_chain(self):
        self.assertEqual(
            gstreamer.encoder_chain("x264enc", 12000, 30, 60),
            "videoconvert n-threads=4 ! video/x-raw,format=I420 ! x264enc "
            "tune=zerolatency speed-preset=ultrafast bitrate=12000 "
            "key-int-max=30 bframes=0 threads=2 sliced-threads=false "
            "byte-stream=true vbv-buf-capacity=33")

    def test_openh264_chain(self):
        self.assertEqual(
            gstreamer.encoder_chain("openh264enc", 4000, 60, 30),
            "videoconvert n-threads=4 ! video/x-raw,format=I420 ! openh264enc "
            "usage-type=screen rate-control=bitrate bitrate=4000000 "
            "gop-size=60 complexity=low")

    def test_va_chain_without_vapostproc_converts_on_the_cpu(self):
        self.assertEqual(
            gstreamer.encoder_chain("vah264enc", 12000, 30, 60),
            "videoconvert n-threads=4 ! video/x-raw,format=NV12 ! vah264enc "
            "rate-control=cbr bitrate=12000 key-int-max=30 b-frames=0 "
            "target-usage=7 cpb-size=400")

    def test_vaapi_chain(self):
        self.assertEqual(
            gstreamer.encoder_chain("vaapih264enc", 8000, 15, 120),
            "videoconvert n-threads=4 ! video/x-raw,format=NV12 ! vaapih264enc "
            "rate-control=cbr bitrate=8000 keyframe-period=15 max-bframes=0 "
            "cpb-length=17")

    def test_every_chain_feeds_a_420_format(self):
        for encoder in gstreamer.ENCODERS:
            with self.subTest(encoder=encoder):
                chain = gstreamer.encoder_chain(encoder, 8000, 30)
                self.assertRegex(chain, r"format=(NV12|I420) ! ")

    def test_unknown_encoder_is_rejected(self):
        with self.assertRaises(ValueError):
            gstreamer.encoder_chain("nvh264enc", 1, 1)

    def test_build_pipeline(self):
        argv = gstreamer.build_pipeline(42, 2400, 1080, 12000, 30, "x264enc",
                                        fd=7, fps=60)
        self.assertEqual(argv[:6], ["gst-launch-1.0", "-q", "pipewiresrc",
                                    "path=42", "do-timestamp=true",
                                    "keepalive-time=1000"])
        description = " ".join(argv[2:])
        self.assertIn("! video/x-raw,width=2400,height=1080 ! "
                      + gstreamer.LEAKY_QUEUE + " ! videoconvert", description)
        self.assertTrue(description.endswith(
            "! h264parse config-interval=-1 "
            "! video/x-h264,stream-format=byte-stream,alignment=au "
            "! fdsink fd=7 sync=false"))

    def test_pipewiresrc_without_keepalive_support(self):
        gstreamer._inspect_cache["pipewiresrc"] = inspect_output()
        self.assertEqual(gstreamer.pipewiresrc(5, "always-copy=true"),
                         "pipewiresrc path=5 do-timestamp=true always-copy=true")


class SpawnPipelineTest(unittest.TestCase):
    """spawn_pipeline with a real child process standing in for gst-launch."""

    def test_stream_carries_what_the_child_writes_to_the_fd(self):
        def description(fd):
            return ["sh", "-c", f"printf 'annexb' >&{fd}; echo 'a warning' >&2"]

        with mock.patch.object(gstreamer, "log") as log:
            proc, stream = gstreamer.spawn_pipeline(description, label="gst")
            try:
                self.assertEqual(stream.read(), b"annexb")   # EOF: child done
                proc.wait(timeout=5)
                deadline = time.monotonic() + 5
                while not log.called and time.monotonic() < deadline:
                    time.sleep(0.01)
            finally:
                stream.close()
        log.assert_called_with("gst: a warning")

    def test_popen_arguments(self):
        with mock.patch.object(gstreamer.subprocess, "Popen") as popen:
            popen.return_value.stderr = io.BytesIO()
            proc, stream = gstreamer.spawn_pipeline(lambda fd: ["gst", str(fd)])
            stream.close()
        (argv,), kwargs = popen.call_args
        write_fd = kwargs["pass_fds"][0]
        self.assertEqual(argv, ["gst", str(write_fd)])
        self.assertEqual(kwargs, {"stdin": subprocess.DEVNULL,
                                  "stdout": subprocess.DEVNULL,
                                  "stderr": subprocess.PIPE,
                                  "pass_fds": (write_fd,),
                                  "start_new_session": True})
        with self.assertRaises(OSError):
            os.fstat(write_fd)              # our copy of the write end closed

    def test_spawn_failure_leaves_no_fd_open(self):
        before = set(os.listdir("/proc/self/fd"))
        with self.assertRaises(FileNotFoundError):
            gstreamer.spawn_pipeline(
                lambda fd: ["/nonexistent/gst-launch-1.0", str(fd)])
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)

    def test_description_failure_leaves_no_fd_open(self):
        def broken(_fd):
            raise ValueError("unsupported encoder")

        before = set(os.listdir("/proc/self/fd"))
        with self.assertRaises(ValueError):
            gstreamer.spawn_pipeline(broken)
        self.assertEqual(set(os.listdir("/proc/self/fd")), before)


class StopPipelineTest(unittest.TestCase):
    def test_none_and_exited_processes_are_left_alone(self):
        gstreamer.stop_pipeline(None)
        exited = mock.Mock()
        exited.poll.return_value = 0
        gstreamer.stop_pipeline(exited)
        exited.terminate.assert_not_called()

    def test_terminate_then_kill_after_three_seconds(self):
        stuck = mock.Mock()
        stuck.poll.return_value = None
        stuck.wait.side_effect = [subprocess.TimeoutExpired("gst", 3), 0]
        gstreamer.stop_pipeline(stuck)
        self.assertEqual([call[0] for call in stuck.method_calls],
                         ["poll", "terminate", "wait", "kill", "wait"])
        self.assertEqual(stuck.wait.call_args_list[0], mock.call(timeout=3))

    def test_real_process_stops(self):
        proc = subprocess.Popen(["sleep", "30"])
        gstreamer.stop_pipeline(proc)
        self.assertIsNotNone(proc.poll())


if __name__ == "__main__":
    unittest.main()
