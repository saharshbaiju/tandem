"""Tests for the TUI parts of issue #6: the Encoder setting reaching the
daemon, the daemon log being saved to a file, and the live view showing log
lines whole instead of cutting them off behind the QR code."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import os
import types
import unittest
from unittest import mock

import core
import tui
from backends import gnome, hyprland


class EncoderSettingTest(unittest.TestCase):
    def test_default_is_auto_and_reaches_the_daemon(self):
        cfg = core.Config(backend="gnome")
        self.assertEqual(cfg.encoder, "auto")
        args = cfg.tandemd_args()
        self.assertEqual(args[args.index("--encoder") + 1], "auto")

    def test_a_chosen_encoder_reaches_the_daemon(self):
        args = core.Config(backend="kde", encoder="x264enc").tandemd_args()
        self.assertEqual(args[args.index("--encoder") + 1], "x264enc")

    def test_choices_follow_the_backend(self):
        with mock.patch.object(gnome, "installed_encoders",
                               return_value=["vah264lpenc", "x264enc"]):
            self.assertEqual(core.Config(backend="gnome").encoder_choices(),
                             ["auto", "vah264lpenc", "x264enc"])
        self.assertEqual(core.Config(backend="hyprland").encoder_choices(),
                         ["auto"] + list(hyprland.ENCODER_CHOICES))

    def test_a_choice_from_another_backend_is_kept(self):
        cfg = core.Config(backend="hyprland", encoder="x264enc")
        self.assertEqual(cfg.encoder_choices()[-1], "x264enc")

    def test_gstreamer_backends_never_raise_for_choices(self):
        with mock.patch.object(gnome, "installed_encoders",
                               side_effect=OSError):
            self.assertEqual(gnome.encoder_choices(), ())

    def test_changing_the_encoder_restarts_the_stream(self):
        self.assertIn("encoder", tui.RESTART_KEYS)

    def test_old_config_files_load_with_auto(self):
        os.makedirs(core.CONFIG_DIR, exist_ok=True)
        with open(core.CONFIG_PATH, "w") as f:
            f.write('{"backend": "kde", "quality": "ultra"}')
        self.addCleanup(os.remove, core.CONFIG_PATH)
        cfg = core.load_config()
        self.assertEqual((cfg.quality, cfg.encoder), ("ultra", "auto"))


class DaemonLogFileTest(unittest.TestCase):
    def setUp(self):
        self.addCleanup(self.remove_logs)
        self.remove_logs()

    @staticmethod
    def remove_logs():
        for path in (core.DAEMON_LOG_PATH, core.DAEMON_LOG_PATH + ".1"):
            if os.path.exists(path):
                os.remove(path)

    def test_the_log_lives_under_the_state_directory(self):
        self.assertTrue(core.DAEMON_LOG_PATH.startswith(support.SCRATCH_HOME))
        self.assertTrue(core.DAEMON_LOG_PATH.endswith(
            os.path.join(".local", "state", "tandem", "tandemd.log")))

    def test_a_new_session_keeps_the_previous_one_as_dot_1(self):
        with core.open_daemon_log(new_session=True) as log:
            log.write("first session\n")
        with core.open_daemon_log(new_session=False) as log:
            log.write("same session\n")
        with core.open_daemon_log(new_session=True) as log:
            log.write("second session\n")
        with open(core.DAEMON_LOG_PATH + ".1") as f:
            self.assertEqual(f.read(), "first session\nsame session\n")
        with open(core.DAEMON_LOG_PATH) as f:
            self.assertEqual(f.read(), "second session\n")

    def test_an_unwritable_log_is_none_not_an_error(self):
        with mock.patch.object(core.os, "makedirs", side_effect=OSError):
            self.assertIsNone(core.open_daemon_log(new_session=True))

    def test_home_relative(self):
        home = os.path.expanduser("~")
        self.assertEqual(core.home_relative(os.path.join(home, "a", "b")),
                         "~/a/b")
        self.assertEqual(core.home_relative("/etc/x"), "/etc/x")


class StreamLogTest(unittest.TestCase):
    """Stream saves every daemon line, across restarts and new Streams."""

    def setUp(self):
        self.addCleanup(DaemonLogFileTest.remove_logs)
        DaemonLogFileTest.remove_logs()
        patcher = mock.patch.dict(tui._active, {"log_started": False})
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_daemon(self, stream, lines):
        stream._start_log_file(["tandemd.py", "--backend", "kde"])
        if stream.log_file is not None:
            self.addCleanup(stream.log_file.close)
        stream.proc = types.SimpleNamespace(stderr=iter(
            line + "\n" for line in lines))
        stream._reader()

    def test_every_line_is_kept_on_screen_and_in_the_file(self):
        stream = tui.Stream(core.Config(backend="kde"))
        lines = [f"[tandemd] line {i}" for i in range(20)]
        self.run_daemon(stream, lines)
        self.assertEqual(list(stream.logs), lines)        # not just the last 8
        with open(core.DAEMON_LOG_PATH) as f:
            saved = f.read().splitlines()
        self.assertTrue(saved[0].startswith("=== Tandem "))
        self.assertTrue(saved[0].endswith("tandemd.py --backend kde"))
        self.assertEqual(saved[1:], lines)

    def test_the_failed_run_survives_starting_again(self):
        first = tui.Stream(core.Config(backend="kde"))
        self.run_daemon(first, ["[tandemd] error: the encoder keeps failing"])
        second = tui.Stream(core.Config(backend="kde"))
        self.run_daemon(second, ["[tandemd] capture started"])
        with open(core.DAEMON_LOG_PATH) as f:
            text = f.read()
        self.assertIn("encoder keeps failing", text)
        self.assertIn("capture started", text)
        self.assertFalse(os.path.exists(core.DAEMON_LOG_PATH + ".1"))

    def test_a_failing_disk_never_stops_the_reader(self):
        stream = tui.Stream(core.Config(backend="kde"))
        stream._start_log_file(["tandemd.py"])
        stream.log_file.close()
        stream.log_file = mock.Mock(write=mock.Mock(side_effect=OSError))
        stream.proc = types.SimpleNamespace(stderr=iter(["a\n", "b\n"]))
        stream._reader()
        self.assertEqual(list(stream.logs), ["a", "b"])
        self.assertIsNone(stream.log_file)


class FakeWindow:
    """Just enough of a curses window to record what is drawn."""

    def __init__(self, height, width):
        self.height, self.width = height, width
        self.rows = {}

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, y, x, text, n, attr=0):
        self.rows[y] = (x, text[:n])


class LiveViewLogTest(unittest.TestCase):
    LONG = ("[tandemd] gst: WARNING: erroneous pipeline: could not set "
            "property \"rate-control\" in element \"vah264lpenc0\" to \"cbr\"")

    def setUp(self):
        patcher = mock.patch.object(tui.curses, "color_pair", return_value=0)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_long_lines_are_wrapped_not_cut(self):
        rows = tui.log_rows([self.LONG], 40)
        self.assertGreater(len(rows), 1)
        self.assertTrue(all(len(row) <= 40 for row in rows))
        self.assertEqual("".join(rows).replace(" ", ""),
                         self.LONG.replace(" ", ""))     # nothing lost
        self.assertTrue(rows[1].startswith("  "))

    def test_newest_lines_fill_the_room_left(self):
        stream = types.SimpleNamespace(
            logs=[f"line {i}" for i in range(50)], log_file=object())
        win = FakeWindow(30, 100)
        end = tui.draw_log(win, 10, 28, 60, stream)
        self.assertIn("all of it: ~/.local/state/tandem/tandemd.log",
                      win.rows[10][1])
        drawn = [win.rows[y][1] for y in range(11, end)]
        self.assertEqual(drawn, [f"line {i}" for i in range(33, 50)])
        self.assertEqual(end, 28)

    def test_no_room_draws_no_lines(self):
        # The old code's [-0:] drew every line when there was no room.
        stream = types.SimpleNamespace(logs=["a", "b"], log_file=None)
        win = FakeWindow(12, 80)
        self.assertEqual(tui.draw_log(win, 11, 12, 60, stream), 12)
        self.assertEqual(list(win.rows), [11])
        self.assertEqual(win.rows[11][1], "daemon log:")


if __name__ == "__main__":
    unittest.main()
