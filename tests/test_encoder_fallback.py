"""Tests for the encoder checks and fallback in backends/gstreamer.py (issue
#6: on Intel GPUs whose VA driver offers no bitrate control, gst-launch
rejected `rate-control=cbr` and Tandem stopped instead of trying another
encoder), plus the version `--version` prints."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import os
import re
import subprocess
import sys
import time
import types
import unittest
from unittest import mock

import about
from backends import gstreamer

# gst-inspect-1.0 vah264lpenc on an Intel GPU whose HuC firmware isn't
# loaded: the driver lists only constant-quantizer rate control.
VA_LOW_POWER_CQP_ONLY = """\
Factory Details:
  Rank                     none (0)
  Long-name                VA-API H.264 Low Power Encoder in Intel(R) Gen Graphics

Element Properties:
  b-frames            : Number of B frames between I and P reference frames
                        flags: readable, writable, changeable only in NULL or READY state
                        Unsigned Integer. Range: 0 - 31 Default: 0
  bitrate             : The desired bitrate expressed in kbps (0: auto-calculate)
                        flags: readable, writable, changeable in NULL, READY, PAUSED or PLAYING state
                        Unsigned Integer. Range: 0 - 2048000 Default: 0
  cpb-size            : The desired max CPB size in Kb (0: auto-calculate)
                        flags: readable, writable, changeable only in NULL or READY state
                        Unsigned Integer. Range: 0 - 2048000 Default: 0
  key-int-max         : The maximal distance between two keyframes. It decides the size of GOP (0: auto-calculate)
                        flags: readable, writable, changeable only in NULL or READY state
                        Unsigned Integer. Range: 0 - 1024 Default: 0
  rate-control        : The desired rate control mode for the encoder
                        flags: readable, writable, changeable in NULL, READY, PAUSED or PLAYING state
                        Enum "GstVaEncoderRateControl_vah264lpenc" Default: 16, "cqp"
                           (16): cqp              - Constant Quantizer
  target-usage        : The target usage to control and balance the encoding speed/quality
                        flags: readable, writable, changeable in NULL, READY, PAUSED or PLAYING state
                        Unsigned Integer. Range: 1 - 7 Default: 4
"""


def with_rate_control(*modes):
    """VA_LOW_POWER_CQP_ONLY with these rate control modes listed instead."""
    numbers = {"cbr": 2, "vbr": 4, "cqp": 16}
    listed = "".join(
        f"                           ({numbers[mode]}): {mode:<16} - {mode}\n"
        for mode in modes)
    return re.sub(r"(?m)^ {27}\(16\): cqp .*\n", listed, VA_LOW_POWER_CQP_ONLY)


# gst-inspect-1.0 x264enc: `tune` is a flags property whose values are hex,
# which must not be mistaken for an enum.
X264_TUNE = """\
  tune                : Preset name for non-psychovisual tuning options
                        flags: readable, writable
                        Flags "GstX264EncTune" Default: 0x00000000, "(none)"
                           (0x00000001): stillimage       - Still image
                           (0x00000004): zerolatency      - Zero latency
  speed-preset        : Preset name for speed/quality tradeoff options
                        flags: readable, writable
                        Enum "GstX264EncPreset" Default: 6, "medium"
                           (1): ultrafast        - ultrafast
                           (6): medium           - medium
"""


class InspectText:
    """Context manager: gst_inspect answers with this text per element;
    every other element is missing."""

    def __init__(self, texts):
        self.texts = texts

    def __enter__(self):
        self.saved = dict(gstreamer._inspect_cache)
        gstreamer._inspect_cache.clear()
        for name in gstreamer.ENCODERS + ("vapostproc",):
            gstreamer._inspect_cache[name] = ""
        gstreamer._inspect_cache.update(self.texts)
        return self

    def __exit__(self, *exc):
        gstreamer._inspect_cache.clear()
        gstreamer._inspect_cache.update(self.saved)


class PropertyDescriptionTest(unittest.TestCase):
    def test_enum_values_lists_only_what_the_driver_offers(self):
        with InspectText({"vah264lpenc": VA_LOW_POWER_CQP_ONLY}):
            self.assertEqual(gstreamer.enum_values("vah264lpenc",
                                                   "rate-control"),
                             {"cqp": "16"})
            self.assertIsNone(gstreamer.enum_values("vah264lpenc", "bitrate"))
            self.assertIsNone(gstreamer.enum_values("vah264lpenc", "nope"))

    def test_integer_range(self):
        with InspectText({"vah264lpenc": VA_LOW_POWER_CQP_ONLY}):
            self.assertEqual(gstreamer.integer_range("vah264lpenc",
                                                     "target-usage"), (1, 7))
            self.assertIsNone(gstreamer.integer_range("vah264lpenc",
                                                      "rate-control"))

    def test_a_block_ends_at_the_next_property(self):
        with InspectText({"vah264lpenc": VA_LOW_POWER_CQP_ONLY}):
            block = gstreamer._property_block("vah264lpenc", "cpb-size")
        self.assertEqual(len(block), 3)
        self.assertTrue(block[0].strip().startswith("cpb-size"))

    def test_flags_are_not_taken_for_an_enum(self):
        with InspectText({"x264enc": X264_TUNE}):
            self.assertIsNone(gstreamer.enum_values("x264enc", "tune"))
            self.assertEqual(gstreamer.setting_value("x264enc", "tune",
                                                     "zerolatency"),
                             "zerolatency")
            self.assertEqual(gstreamer.enum_values("x264enc", "speed-preset"),
                             {"ultrafast": "1", "medium": "6"})


class SettingValueTest(unittest.TestCase):
    def setUp(self):
        self.inspect = InspectText({"vah264lpenc": VA_LOW_POWER_CQP_ONLY})
        self.inspect.__enter__()
        self.addCleanup(self.inspect.__exit__)

    def test_enum_by_name_or_number(self):
        self.assertEqual(gstreamer.setting_value("vah264lpenc",
                                                 "rate-control", "cqp"), "cqp")
        self.assertEqual(gstreamer.setting_value("vah264lpenc",
                                                 "rate-control", "16"), "16")
        self.assertIsNone(gstreamer.setting_value("vah264lpenc",
                                                  "rate-control", "cbr"))

    def test_integers_are_clamped_into_range(self):
        self.assertEqual(gstreamer.setting_value("vah264lpenc",
                                                 "target-usage", 9), "7")
        self.assertEqual(gstreamer.setting_value("vah264lpenc",
                                                 "target-usage", 0), "1")
        self.assertEqual(gstreamer.setting_value("vah264lpenc",
                                                 "key-int-max", 30), "30")
        self.assertIsNone(gstreamer.setting_value("vah264lpenc",
                                                  "target-usage", "fast"))

    def test_undescribed_properties_pass_unchanged(self):
        self.assertEqual(gstreamer.setting_value("vah264lpenc", "unknown",
                                                 "x"), "x")
        self.assertEqual(gstreamer.setting_value("missing-element", "a", 1),
                         "1")


class VaChainTest(unittest.TestCase):
    """The pipeline issue #6 failed on, and what happens to it now."""

    def chain(self, text, encoder="vah264lpenc"):
        with InspectText({encoder: text, "vapostproc": "x",
                          "videoconvert": ""}):
            return gstreamer.encoder_chain(encoder, 12000, 30, 60)

    def test_cbr_is_used_when_offered(self):
        self.assertIn("vah264lpenc rate-control=cbr bitrate=12000 "
                      "key-int-max=30 b-frames=0 target-usage=7 cpb-size=400",
                      self.chain(with_rate_control("cbr", "vbr", "cqp")))

    def test_vbr_when_cbr_is_missing(self):
        self.assertIn("rate-control=vbr",
                      self.chain(with_rate_control("vbr", "cqp")))

    def test_no_bitrate_control_makes_the_encoder_unusable(self):
        with self.assertRaisesRegex(gstreamer.EncoderUnusable,
                                    "no bitrate control.*cqp.*HuC"):
            self.chain(VA_LOW_POWER_CQP_ONLY)

    def test_vaapi_encoder_is_checked_the_same_way(self):
        text = VA_LOW_POWER_CQP_ONLY.replace("vah264lpenc", "vaapih264enc")
        with self.assertRaises(gstreamer.EncoderUnusable):
            self.chain(text, "vaapih264enc")

    def test_target_usage_is_clamped_to_the_driver_range(self):
        text = with_rate_control("cbr").replace("Range: 1 - 7 Default: 4",
                                                "Range: 1 - 4 Default: 4")
        self.assertIn("target-usage=4", self.chain(text))

    def test_required_value_the_element_rejects_is_unusable(self):
        with InspectText({"x264enc": X264_TUNE.replace(
                "(1): ultrafast        - ultrafast\n", "")}):
            with self.assertRaisesRegex(gstreamer.EncoderUnusable,
                                        "speed-preset=ultrafast"):
                gstreamer.encoder_chain("x264enc", 8000, 30)


class CandidatesTest(unittest.TestCase):
    INSTALLED = {"vah264lpenc": "x", "x264enc": "x", "openh264enc": "x"}

    def test_auto_is_every_installed_encoder_hardware_first(self):
        with InspectText(self.INSTALLED):
            self.assertEqual(gstreamer.encoder_candidates(),
                             ["vah264lpenc", "x264enc", "openh264enc"])
            self.assertEqual(gstreamer.encoder_candidates("auto"),
                             ["vah264lpenc", "x264enc", "openh264enc"])

    def test_a_request_goes_first_and_keeps_the_fallbacks(self):
        with InspectText(self.INSTALLED):
            self.assertEqual(gstreamer.encoder_candidates("x264enc"),
                             ["x264enc", "vah264lpenc", "openh264enc"])

    def test_a_request_that_cant_be_honoured_is_logged(self):
        with InspectText(self.INSTALLED), \
                mock.patch.object(gstreamer, "log") as log:
            self.assertEqual(gstreamer.encoder_candidates("vah264enc"),
                             ["vah264lpenc", "x264enc", "openh264enc"])
            self.assertEqual(gstreamer.encoder_candidates("h264_nvenc"),
                             ["vah264lpenc", "x264enc", "openh264enc"])
        self.assertIn("isn't installed", log.call_args_list[0].args[0])
        self.assertIn("isn't one Tandem can use", log.call_args_list[1].args[0])

    def test_choose_encoder_honours_a_missing_request_with_a_fallback(self):
        args = types.SimpleNamespace(codec="h264", encoder="vah264enc")
        with InspectText(self.INSTALLED), mock.patch.object(gstreamer, "log"):
            self.assertEqual(gstreamer.choose_encoder(args, "KDE"),
                             "vah264lpenc")


def fake_process():
    proc = mock.Mock()
    proc.returncode = None
    proc.poll.return_value = None
    return proc


def end(proc, returncode):
    """Make fake `proc` look exited with `returncode`."""
    proc.returncode = returncode
    proc.poll.return_value = returncode


class EncoderSelectorTest(unittest.TestCase):
    """EncoderSelector with the encoder start faked: `unusable` names the
    encoders whose settings the element rejects."""

    def setUp(self):
        self.inspect = InspectText({"vah264lpenc": "x", "x264enc": "x",
                                    "openh264enc": "x"})
        self.inspect.__enter__()
        self.addCleanup(self.inspect.__exit__)
        self.unusable = set()
        self.started = []
        args = types.SimpleNamespace(codec="h264", encoder="auto")
        self.selector = gstreamer.EncoderSelector(args, "KDE")
        for patcher in (mock.patch.object(gstreamer, "start_encoder",
                                          side_effect=self.start_encoder),
                        mock.patch.object(gstreamer, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def start_encoder(self, node, args, encoder):
        self.started.append(encoder)
        if encoder in self.unusable:
            raise gstreamer.EncoderUnusable(f"{encoder} offers no bitrate "
                                            "control")
        return fake_process(), mock.Mock(name=f"{encoder}-stream")

    def logged(self):
        return [call.args[0] for call in gstreamer.log.call_args_list]

    def restart(self):
        """What supervise_capture does after the stream ended: start again."""
        self.started.clear()
        return self.selector.start(42)

    def test_the_first_encoder_is_started_without_waiting(self):
        proc, _stream = self.selector.start(42)
        self.assertEqual(self.started, ["vah264lpenc"])
        self.assertEqual(self.selector.working, "vah264lpenc")
        proc.poll.assert_not_called()

    def test_issue_6_an_unusable_hardware_encoder_falls_back_at_once(self):
        self.unusable.add("vah264lpenc")
        self.selector.start(42)
        self.assertEqual(self.started, ["vah264lpenc", "x264enc"])
        self.assertEqual(self.selector.working, "x264enc")
        self.assertIn("encoder vah264lpenc can't be used: vah264lpenc offers "
                      "no bitrate control; trying the next one", self.logged())
        self.assertIn("using encoder x264enc", self.logged())

    def test_a_restart_goes_straight_back_to_the_working_encoder(self):
        self.unusable.add("vah264lpenc")
        self.selector.start(42)
        self.assertEqual(self.restart()[0].poll(), None)
        self.assertEqual(self.started, ["x264enc"])

    def test_an_encoder_that_fails_right_after_starting_is_skipped(self):
        proc, _stream = self.selector.start(42)
        end(proc, 1)                      # gst-launch: erroneous pipeline
        self.restart()
        self.assertEqual(self.started, ["x264enc"])
        self.assertIn("encoder vah264lpenc failed right after starting (exit "
                      "code 1); trying the next one", self.logged())

    def test_a_crash_counts_as_an_error(self):
        proc, _stream = self.selector.start(42)
        end(proc, -11)                    # SIGSEGV
        self.restart()
        self.assertEqual(self.started, ["x264enc"])

    def test_an_encoder_we_stopped_is_not_skipped(self):
        for returncode in (-15, -9, 0):   # SIGTERM, SIGKILL, clean exit
            with self.subTest(returncode=returncode):
                proc, _stream = self.selector.start(42)
                end(proc, returncode)
                self.restart()
                self.assertEqual(self.started, ["vah264lpenc"])

    def test_an_encoder_that_ran_a_while_is_not_skipped(self):
        proc, _stream = self.selector.start(42)
        end(proc, 1)
        self.selector._last = (proc, time.monotonic() - 60)
        self.restart()
        self.assertEqual(self.started, ["vah264lpenc"])

    def test_every_encoder_gets_its_turn_then_failed_ones_again(self):
        order = []
        for _ in range(4):
            proc, _stream = self.selector.start(42)
            order.append(self.selector.working)
            end(proc, 1)
        self.assertEqual(order, ["vah264lpenc", "x264enc", "openh264enc",
                                 "vah264lpenc"])

    def test_nothing_usable_is_a_clear_error(self):
        self.unusable.update({"vah264lpenc", "x264enc", "openh264enc"})
        with self.assertRaisesRegex(RuntimeError,
                                    "tried: vah264lpenc, x264enc, openh264enc"):
            self.selector.start(42)


class EndedWithAnErrorTest(unittest.TestCase):
    """ended_with_an_error() against real processes."""

    def run_to_end(self, script):
        proc = subprocess.Popen([sys.executable, "-c", script])
        proc.wait()
        return proc

    def test_an_error_exit(self):
        self.assertTrue(gstreamer.ended_with_an_error(
            self.run_to_end("import sys; sys.exit(1)")))

    def test_a_clean_exit(self):
        self.assertFalse(gstreamer.ended_with_an_error(
            self.run_to_end("pass")))

    def test_stopped_by_stop_pipeline(self):
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"])
        gstreamer.stop_pipeline(proc)
        self.assertFalse(gstreamer.ended_with_an_error(proc))

    def test_still_running(self):
        proc = subprocess.Popen([sys.executable, "-c",
                                 "import time; time.sleep(30)"])
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        self.assertFalse(gstreamer.ended_with_an_error(proc))


class VersionTest(unittest.TestCase):
    def test_version_matches_the_android_app(self):
        gradle = os.path.join(support.REPO_DIR, "android", "app",
                              "build.gradle.kts")
        with open(gradle) as f:
            version_name = re.search(r'versionName\s*=\s*"([^"]+)"',
                                     f.read()).group(1)
        self.assertEqual(about.VERSION, version_name)

    def test_version_text_names_the_version_and_keeps_the_credit(self):
        lines = about.version_text().splitlines()
        self.assertEqual(lines[0], f"Tandem {about.VERSION}")
        self.assertIn(about.CREDIT, lines)
        self.assertIn(about.LICENSE, lines)


if __name__ == "__main__":
    unittest.main()
