"""Tests for the KDE backend's helper driving, touch surface, setup retries,
capture guard and module hooks (backends/kde.py).

The helper is replaced by a small Python script that speaks the same line
protocol as tandem-kwin-helper, so the real KwinHelper (subprocess, reader
threads, stdin writes) is exercised without KWin.
"""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import os
import shutil
import stat
import sys
import tempfile
import textwrap
import time
import types
import unittest
from unittest import mock

from backends import gstreamer, kde

# Behaviour per output name, from FAKE_KWIN_SCRIPT (name=event;event|...):
# each event is a line the fake prints, "exit" ends it at once (as the real
# helper does after failed/closed), "pause" waits 0.3 s, "refuse-once"
# prints a refused "ready" the first time it runs (keeps a marker file).
FAKE_HELPER = textwrap.dedent("""\
    import os, sys, time
    name = sys.argv[sys.argv.index("--name") + 1]
    log = open(os.environ["FAKE_KWIN_LOG"], "a")
    log.write("start " + " ".join(sys.argv[1:]) + "\\n")
    log.flush()
    plans = dict(item.split("=", 1)
                 for item in os.environ["FAKE_KWIN_SCRIPT"].split("|"))
    events = plans.get(name, plans.get("*", "")).split(";")
    if events[0] == "refuse-once":
        marker = os.environ["FAKE_KWIN_LOG"] + ".refused"
        events = events[1:]
        if not os.path.exists(marker):
            open(marker, "w").close()
            print("ready screencast=0 fake_input=0", flush=True)
            print("failed KWin does not offer it", flush=True)
            sys.exit(2)
    for event in events:
        if event == "exit":
            sys.exit(2)
        if event == "pause":
            time.sleep(0.3)
            continue
        if event:
            print(event, flush=True)
    for line in sys.stdin:
        log.write("cmd " + line)
        log.flush()
    log.write("eof\\n")
""")

CREATED = "ready screencast=5 fake_input=5;created 42;geometry 1920 0 1600 720"


class FakeHelperCase(unittest.TestCase):
    def setUp(self):
        self.work = tempfile.mkdtemp(prefix="tandem-kde-test-")
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        script = os.path.join(self.work, "fake-helper.py")
        with open(script, "w") as source:
            source.write(FAKE_HELPER)
        self.binary = os.path.join(self.work, "fake-helper")
        with open(self.binary, "w") as wrapper:
            wrapper.write(f"#!/bin/sh\nexec {sys.executable} {script} \"$@\"\n")
        os.chmod(self.binary, stat.S_IRWXU)
        self.log_path = os.path.join(self.work, "helper.log")
        self.helpers = []
        # The daemon's log lines are not what these tests look at.
        for patcher in (mock.patch.object(kde, "log"),
                        mock.patch.object(gstreamer, "log")):
            patcher.start()
            self.addCleanup(patcher.stop)

    def use_script(self, **plans):
        """plans: output name (or "_any") -> ";"-joined events."""
        script = "|".join(f"{'*' if name == '_any' else name}={events}"
                          for name, events in plans.items())
        patcher = mock.patch.dict(os.environ, {"FAKE_KWIN_LOG": self.log_path,
                                               "FAKE_KWIN_SCRIPT": script})
        patcher.start()
        self.addCleanup(patcher.stop)

    def start_helper(self, name="Tandem"):
        helper = kde.KwinHelper(self.binary, name, 2400, 1080)
        self.helpers.append(helper)
        self.addCleanup(helper.stop)
        return helper

    def helper_log(self):
        try:
            with open(self.log_path) as log:
                return log.read().splitlines()
        except FileNotFoundError:
            return []

    def wait_for_log(self, line, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if line in self.helper_log():
                return True
            time.sleep(0.02)
        return False


class ParseEventTest(unittest.TestCase):
    def test_every_event(self):
        cases = {
            "ready screencast=5 fake_input=0": ("ready", (5, 0)),
            "created 42": ("created", 42),
            "geometry -10 0 1600 720": ("geometry", (-10, 0, 1600, 720)),
            "failed Could not find output": ("failed", "Could not find output"),
            "closed": ("closed", None),
            "error lost the connection to KWin (Broken pipe)":
                ("error", "lost the connection to KWin (Broken pipe)"),
        }
        for line, event in cases.items():
            with self.subTest(line=line):
                self.assertEqual(kde.parse_event(line + "\n"), event)

    def test_malformed_lines(self):
        for line in ("", "ready", "ready screencast=x fake_input=1",
                     "created", "created node", "geometry 1 2 3",
                     "closed now", "hello"):
            with self.subTest(line=line):
                self.assertIsNone(kde.parse_event(line))


class KwinHelperTest(FakeHelperCase):
    def test_created_output_is_tracked_and_commands_reach_stdin(self):
        self.use_script(Tandem=CREATED)
        helper = self.start_helper()
        self.assertTrue(helper.wait_for_outcome(5))
        self.assertEqual(helper.node, 42)
        self.assertEqual((helper.screencast_version,
                          helper.fake_input_version), (5, 5))
        self.assertEqual(helper.output_name, "Virtual-Tandem")
        self.assertTrue(helper.alive)
        helper.send("down 0 0.500000 0.500000")
        self.assertTrue(self.wait_for_log("cmd down 0 0.500000 0.500000"))
        self.assertIn("start --name Tandem --width 2400 --height 1080",
                      self.helper_log())
        helper.stop()
        self.assertTrue(self.wait_for_log("eof"))
        helper.stop()                                   # idempotent
        self.assertFalse(helper.alive)
        with self.assertRaises(RuntimeError):
            helper.send("up 0")

    def test_failure_and_refusal_end_the_wait_without_an_output(self):
        self.use_script(Tandem="ready screencast=5 fake_input=5;"
                               "failed Could not find output;exit")
        failed = self.start_helper()
        self.assertFalse(failed.wait_for_outcome(5))
        self.assertEqual(failed.failure, "Could not find output")

    def test_refused_screencast(self):
        self.use_script(Tandem="ready screencast=0 fake_input=0;"
                               "failed KWin does not offer it;exit")
        refused = self.start_helper()
        self.assertFalse(refused.wait_for_outcome(5))
        self.assertTrue(refused.refused_screencast)

    def test_a_silent_helper_times_out(self):
        self.use_script(Tandem="ready screencast=5 fake_input=5")
        helper = self.start_helper()
        started = time.monotonic()
        self.assertFalse(helper.wait_for_outcome(0.3))
        self.assertLess(time.monotonic() - started, 2)

    def test_closed_after_creation_is_not_alive(self):
        self.use_script(Tandem=CREATED + ";closed;exit")
        helper = self.start_helper()
        helper.wait_for_outcome(5)
        deadline = time.monotonic() + 3
        while helper.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(helper.closed)
        self.assertFalse(helper.alive)

    def test_missing_binary_is_a_runtime_error(self):
        with self.assertRaises(RuntimeError):
            kde.KwinHelper(os.path.join(self.work, "nope"), "Tandem", 1, 1)


class RecordingHelper:
    output_name = "Virtual-Tandem"

    def __init__(self, fail=False):
        self.sent = []
        self.fail = fail

    def send(self, command):
        if self.fail:
            raise RuntimeError("the KWin helper has stopped")
        self.sent.append(command)


class KwinTouchscreenTest(unittest.TestCase):
    def test_commands_are_clamped_fractions(self):
        helper = RecordingHelper()
        screen = kde.KwinTouchscreen(helper)
        self.assertEqual(screen.slot_count, 10)
        self.assertEqual(screen.description,
                         "KWin fake input on Virtual-Tandem")
        screen.down(0, 0.25, 0.5)
        screen.move(0, 1.7, -0.2)
        screen.up(0)
        self.assertEqual(helper.sent, ["down 0 0.250000 0.500000",
                                       "move 0 1.000000 0.000000", "up 0"])

    def test_moves_and_ups_of_inactive_slots_are_ignored(self):
        helper = RecordingHelper()
        screen = kde.KwinTouchscreen(helper)
        screen.move(3, 0.5, 0.5)
        screen.up(3)
        self.assertEqual(helper.sent, [])

    def test_close_lifts_every_finger_once(self):
        helper = RecordingHelper()
        screen = kde.KwinTouchscreen(helper)
        screen.down(2, 0.1, 0.1)
        screen.down(1, 0.2, 0.2)
        screen.close()
        screen.close()
        screen.down(0, 0.5, 0.5)                       # after close: ignored
        self.assertEqual(helper.sent[2:], ["up 1", "up 2"])
        self.assertEqual(len(helper.sent), 4)

    def test_close_with_a_dead_helper_does_not_raise(self):
        helper = RecordingHelper()
        screen = kde.KwinTouchscreen(helper)
        screen.down(0, 0.5, 0.5)
        helper.fail = True
        screen.close()

    def test_sends_with_a_dead_helper_raise(self):
        screen = kde.KwinTouchscreen(RecordingHelper(fail=True))
        with self.assertRaises(RuntimeError):
            screen.down(0, 0.5, 0.5)


def backend_args(**overrides):
    values = dict(width=2400, height=1080, fps=60, scale=1.5, side="right",
                  bitrate="12M", codec="h264", gop=30, encoder="auto",
                  touch=True, keep_output=False)
    values.update(overrides)
    return types.SimpleNamespace(**values)


class KdeBackendSetupTest(FakeHelperCase):
    def setUp(self):
        super().setUp()
        for name, value in (("ensure_built", self.binary),
                            ("ensure_desktop_entry", False)):
            patcher = mock.patch.object(kde.kwin_helper, name,
                                        return_value=value)
            patcher.start()
            self.addCleanup(patcher.stop)
        # Placement is covered by test_kde_layout; here kscreen-doctor only
        # has to list the output.
        patcher = mock.patch.object(kde, "query_outputs",
                                    side_effect=self.fake_outputs)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.placed = []
        patcher = mock.patch.object(kde.KdeBackend, "_place",
                                    side_effect=self.fake_place,
                                    autospec=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"})
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_outputs(self):
        return [kde.KscreenOutput(f"Virtual-{name}", True, True, 0, 0, 2400,
                                  1080, 1.0, 1, 2, 60.0)
                for name in ("Tandem", "Tandem-2", "Tandem-3")]

    def fake_place(self, backend, side, scale):
        self.placed.append((backend.helper.output_name, side, scale))
        return True

    def backend(self, **overrides):
        backend = kde.KdeBackend(backend_args(**overrides))
        self.addCleanup(backend.teardown)
        return backend

    def test_setup_creates_and_places_the_output(self):
        self.use_script(Tandem=CREATED)
        backend = self.backend()
        backend.setup()
        self.assertEqual(backend.helper.node, 42)
        self.assertEqual(self.placed, [("Virtual-Tandem", "right", 1.5)])
        backend.teardown()
        self.assertIsNone(backend.helper)
        self.assertTrue(self.wait_for_log("eof"))
        backend.teardown()                              # idempotent

    def test_bug_527747_retries_with_the_next_name(self):
        self.use_script(Tandem="ready screencast=5 fake_input=5;"
                               "failed Could not find output;exit",
                        _any=CREATED)
        backend = self.backend()
        backend.setup()
        self.assertEqual(backend.helper.output_name, "Virtual-Tandem-2")
        starts = [line for line in self.helper_log()
                  if line.startswith("start")]
        self.assertEqual(len(starts), 2)

    def test_gives_up_after_five_names(self):
        self.use_script(_any="ready screencast=5 fake_input=5;"
                             "failed Could not find output;exit")
        backend = self.backend()
        with self.assertRaises(RuntimeError) as raised:
            backend.setup()
        self.assertIn("527747", str(raised.exception))
        self.assertEqual(sum(line.startswith("start")
                             for line in self.helper_log()), 5)
        self.assertIsNone(backend.helper)

    def test_a_translated_refusal_also_retries_with_the_next_name(self):
        # KWin sends its reason through i18n(): on a German Plasma bug
        # 527747 arrives as "Ausgabe kann nicht gefunden werden".
        self.use_script(Tandem="ready screencast=5 fake_input=5;"
                               "failed Ausgabe kann nicht gefunden werden;"
                               "exit",
                        _any=CREATED)
        backend = self.backend()
        backend.setup()
        self.assertEqual(backend.helper.output_name, "Virtual-Tandem-2")

    def test_a_translated_refusal_on_every_name_still_explains_527747(self):
        self.use_script(_any="ready screencast=5 fake_input=5;"
                             "failed Ausgabe kann nicht gefunden werden;exit")
        with self.assertRaises(RuntimeError) as raised:
            self.backend().setup()
        self.assertIn("527747", str(raised.exception))
        self.assertIn("Ausgabe kann nicht gefunden werden",
                      str(raised.exception))

    def test_other_failures_report_kwins_reason(self):
        self.use_script(_any="ready screencast=5 fake_input=5;"
                             "failed Unsupported compositing type;exit")
        with self.assertRaises(RuntimeError) as raised:
            self.backend().setup()
        self.assertIn("Unsupported compositing type", str(raised.exception))
        self.assertEqual(sum(line.startswith("start")
                             for line in self.helper_log()),
                         kde.MAX_NAME_ATTEMPTS)

    def test_refused_protocol_names_the_desktop_entry(self):
        self.use_script(_any="ready screencast=0 fake_input=0;"
                             "failed KWin does not offer it;exit")
        with self.assertRaises(RuntimeError) as raised:
            self.backend().setup()
        self.assertIn("tandem-kwin-helper.desktop", str(raised.exception))
        self.assertIn("log out", str(raised.exception))

    def test_a_just_written_entry_gets_one_more_try(self):
        self.use_script(_any="refuse-once;" + CREATED)
        kde.kwin_helper.ensure_desktop_entry.return_value = True
        with mock.patch.object(kde, "PERMISSION_RETRY_DELAY", 0.0):
            backend = self.backend()
            backend.setup()
        self.assertEqual(backend.helper.node, 42)

    def test_x11_is_refused_before_anything_starts(self):
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": ""}):
            with self.assertRaises(RuntimeError) as raised:
                self.backend().setup()
        self.assertIn("X11", str(raised.exception))
        kde.kwin_helper.ensure_built.assert_not_called()

    def test_apply_live_reports_success(self):
        # tandemd's control thread tells the TUI whether the change applied.
        self.use_script(Tandem=CREATED)
        backend = self.backend()
        backend.setup()
        self.assertIs(backend.apply_live("left", 2.0), True)
        self.assertEqual(self.placed[-1], ("Virtual-Tandem", "left", 2.0))
        kde.KdeBackend._place.side_effect = RuntimeError("kscreen says no")
        self.assertIs(backend.apply_live("up", 2.0), False)

    def test_a_placement_error_is_only_a_warning(self):
        self.use_script(Tandem=CREATED)
        kde.KdeBackend._place.side_effect = RuntimeError("kscreen says no")
        backend = self.backend()
        backend.setup()
        self.assertTrue(backend.helper.alive)

    def test_an_unexpected_error_after_start_stops_the_helper(self):
        self.use_script(Tandem=CREATED)
        kde.KdeBackend._place.side_effect = KeyError("bug")
        backend = self.backend()
        with self.assertRaises(KeyError):
            backend.setup()
        self.assertIsNone(backend.helper)
        self.assertTrue(self.wait_for_log("eof"))

    def test_touch_surface_needs_fake_input(self):
        self.use_script(Tandem="ready screencast=5 fake_input=0;created 7")
        backend = self.backend()
        backend.setup()
        with self.assertRaises(RuntimeError) as raised:
            backend.create_touch_surface()
        self.assertIn("org_kde_kwin_fake_input", str(raised.exception))

    def test_touch_surface_talks_to_the_helper(self):
        self.use_script(Tandem=CREATED)
        backend = self.backend()
        backend.setup()
        screen = backend.create_touch_surface()
        screen.down(1, 0.0, 1.0)
        screen.close()
        self.assertTrue(self.wait_for_log("cmd up 1"))
        self.assertIn("cmd down 1 0.000000 1.000000", self.helper_log())

    def test_capture_refuses_once_the_output_is_closed(self):
        self.use_script(Tandem=CREATED + ";pause;closed;exit")
        backend = self.backend()
        backend.setup()
        deadline = time.monotonic() + 3
        while backend.helper.alive and time.monotonic() < deadline:
            time.sleep(0.02)
        with mock.patch.object(gstreamer, "spawn_pipeline") as spawn:
            with self.assertRaises(RuntimeError) as raised:
                backend.start_capture()
        spawn.assert_not_called()
        self.assertIn("virtual output was closed", str(raised.exception))
        self.assertFalse(backend.apply_live("left", 1.0))

    def test_capture_pipeline_is_pinned_to_the_requested_size(self):
        self.use_script(Tandem=CREATED)
        backend = self.backend()
        backend.setup()
        process = mock.Mock()
        with mock.patch.object(gstreamer, "pick_encoder", return_value="x264enc"), \
                mock.patch.object(gstreamer, "build_pipeline",
                                  return_value=["gst-launch-1.0"]) as build, \
                mock.patch.object(gstreamer, "spawn_pipeline",
                                  side_effect=lambda describe: (
                                      process, describe(9))) as spawn:
            stream = backend.start_capture()
        spawn.assert_called_once()
        self.assertEqual(stream, ["gst-launch-1.0"])
        self.assertEqual(build.call_args.args[:3], (42, 2400, 1080))
        self.assertEqual(build.call_args.kwargs["fd"], 9)
        self.assertIs(backend.proc, process)
        with mock.patch.object(kde, "stop_pipeline") as stop:
            backend.stop_capture()
            backend.stop_capture()
        stop.assert_has_calls([mock.call(process), mock.call(None)])

    def test_no_encoder_is_a_clear_error(self):
        self.use_script(Tandem=CREATED)
        backend = self.backend()
        backend.setup()
        with mock.patch.object(gstreamer, "pick_encoder", return_value=None):
            with self.assertRaises(RuntimeError) as raised:
                backend.start_capture()
        self.assertIn("H.264 encoder", str(raised.exception))


class KdeHooksTest(unittest.TestCase):
    def test_session_confidence(self):
        cases = {"KDE": 2, "kde": 2, "ubuntu:KDE": 2, "GNOME": 0,
                 "Hyprland": 0, "": 0, "KDEish": 0}
        for desktop, confidence in cases.items():
            with self.subTest(desktop=desktop), \
                    mock.patch.dict(os.environ,
                                    {"XDG_CURRENT_DESKTOP": desktop,
                                     "WAYLAND_DISPLAY": "wayland-0"}):
                self.assertEqual(kde.session_confidence(), confidence)

    def test_session_confidence_on_x11_is_only_the_name(self):
        # Like GNOME on X11 (the contract in backends/__init__.py): 1.
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "KDE",
                                          "WAYLAND_DISPLAY": ""}):
            self.assertEqual(kde.session_confidence(), 1)

    def test_session_problem_on_x11_only(self):
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
            self.assertIsNone(kde.session_problem())
        with mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": ""}):
            lines = kde.session_problem()
        self.assertIn("X11", lines[0])

    def test_hooks_without_work(self):
        self.assertIsNone(kde.remove_leftover_output("EXT-1"))
        self.assertIsNone(kde.touch_setup_problem())
        self.assertFalse(kde.KdeBackend.honors_keep_output)
        self.assertEqual(kde.KdeBackend.name, "kde")

    def test_missing_lists_kscreen_doctor_and_build_tools(self):
        with mock.patch.object(gstreamer.shutil, "which", return_value=None), \
                mock.patch.object(kde.kwin_helper, "is_current",
                                  return_value=False), \
                mock.patch.object(kde.kwin_helper, "build_problem",
                                  return_value=["wayland-scanner"]):
            missing = kde.KdeBackend.missing()
        self.assertEqual(missing, ["kscreen-doctor", "gst-launch-1.0",
                                   "gst-inspect-1.0",
                                   "to build the KWin helper: wayland-scanner"])


if __name__ == "__main__":
    unittest.main()
