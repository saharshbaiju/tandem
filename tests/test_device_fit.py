"""Tests for fitting the virtual monitor to the phone watching it
(device_fit.py, tandemd's SIZE handling and resize_monitor, the backends'
resize(), and the Fit to device setting in the TUI)."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import json
import os
import threading
import time
import types
import unittest
from unittest import mock

import core
import device_fit
import tandemd
import tui
from backends import gnome, hyprland, kde


class ParseSizeTest(unittest.TestCase):
    def test_a_size_frame(self):
        self.assertEqual(device_fit.parse_size(b'{"width":2400,"height":1080}'),
                         (2400, 1080))

    def test_anything_else_is_none(self):
        for body in (b"", b"not json", b"[]", b'{"width":2400}',
                     b'{"width":"2400","height":1080}',
                     b'{"width":true,"height":1080}',
                     b'{"width":0,"height":1080}',
                     b'{"width":-5,"height":1080}',
                     b'{"width":99999,"height":1080}'):
            self.assertIsNone(device_fit.parse_size(body), body)


class FitSizeTest(unittest.TestCase):
    def test_a_phone_keeps_its_exact_size(self):
        self.assertEqual(device_fit.fit_size(2400, 1080), (2400, 1080))

    def test_a_tablet_keeps_its_exact_size(self):
        self.assertEqual(device_fit.fit_size(2560, 1600), (2560, 1600))

    def test_odd_sizes_become_even(self):
        self.assertEqual(device_fit.fit_size(2399, 1081), (2398, 1080))

    def test_a_huge_screen_is_scaled_down_keeping_its_shape(self):
        w, h = device_fit.fit_size(7680, 3456)
        self.assertLessEqual(max(w, h), device_fit.MAX_LONG_SIDE)
        self.assertLessEqual(w * h, device_fit.MAX_AREA)
        self.assertAlmostEqual(w / h, 7680 / 3456, places=2)
        self.assertEqual((w % 2, h % 2), (0, 0))

    def test_a_square_screen_is_capped_by_area(self):
        w, h = device_fit.fit_size(3840, 3840)
        self.assertLessEqual(w * h, device_fit.MAX_AREA)
        self.assertEqual(w, h)

    def test_too_small_is_none(self):
        self.assertIsNone(device_fit.fit_size(300, 200))


class LastSizeTest(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(support.SCRATCH_HOME, "device-size-test.json")
        self.addCleanup(lambda: os.path.exists(self.path)
                        and os.remove(self.path))

    def test_saved_then_loaded(self):
        device_fit.save_last_size((2400, 1080), self.path)
        self.assertEqual(device_fit.load_last_size(self.path), (2400, 1080))

    def test_missing_or_broken_is_none(self):
        self.assertIsNone(device_fit.load_last_size(self.path))
        with open(self.path, "w") as f:
            f.write("{broken")
        self.assertIsNone(device_fit.load_last_size(self.path))

    def test_the_default_path_is_in_the_state_directory(self):
        self.assertEqual(os.path.dirname(device_fit.STATE_PATH),
                         os.path.dirname(core.DAEMON_LOG_PATH))


class DeviceFitTest(unittest.TestCase):
    def setUp(self):
        self.size = (1920, 1080)
        self.restarts = threading.Semaphore(0)
        self.logged = []
        self.fit = device_fit.DeviceFit(lambda: self.size,
                                        self.restarts.release,
                                        self.logged.append, settle=0.05)
        self.addCleanup(self.fit.stop)

    def restarted(self, timeout=1.0):
        return self.restarts.acquire(timeout=timeout)

    def test_the_only_phone_gets_the_monitor_fitted(self):
        self.fit.on_size("10.0.0.5", 2400, 1080, True)
        self.assertTrue(self.restarted())
        self.assertEqual(self.fit.take_wanted(), (2400, 1080))
        self.assertIsNone(self.fit.take_wanted())        # taken once
        self.assertIn("fitting the monitor to 10.0.0.5's screen: "
                      "1920x1080 -> 2400x1080", self.logged)

    def test_a_burst_of_reports_resizes_once_to_the_last(self):
        for width in (2000, 2200, 2400):
            self.fit.on_size("phone", width, 1080, True)
            time.sleep(0.01)
        self.assertTrue(self.restarted())
        self.assertEqual(self.fit.take_wanted(), (2400, 1080))
        self.assertFalse(self.restarted(timeout=0.2))

    def test_the_same_size_changes_nothing(self):
        self.fit.on_size("phone", 1920, 1080, True)
        self.assertFalse(self.restarted(timeout=0.2))

    def test_with_two_phones_the_monitor_stays(self):
        self.fit.on_size("phone", 2400, 1080, False)
        self.assertFalse(self.restarted(timeout=0.2))
        self.assertIsNone(self.fit.take_wanted())
        self.assertIn("more than one phone is connected", self.logged[0])

    def test_a_bogus_size_is_ignored(self):
        self.fit.on_size("phone", 100, 50, True)
        self.assertFalse(self.restarted(timeout=0.2))
        self.assertIn("too small", self.logged[0])


class SizeFrameTest(unittest.TestCase):
    """Broadcaster passes a SIZE frame on, saying if the phone is alone."""

    def setUp(self):
        self.b = tandemd.Broadcaster(b"{}")
        self.reports = []
        self.b.on_device_size = lambda *report: self.reports.append(report)
        self.phone = types.SimpleNamespace(addr=("10.0.0.5", 4000))

    def send(self, body):
        self.b._on_client_message(self.phone, device_fit.FRAME_SIZE, body)

    def test_the_only_phone(self):
        self.b._clients = [self.phone]
        self.send(b'{"width":2400,"height":1080}')
        self.assertEqual(self.reports, [("10.0.0.5", 2400, 1080, True)])

    def test_one_of_two_phones(self):
        self.b._clients = [self.phone, object()]
        self.send(b'{"width":2400,"height":1080}')
        self.assertEqual(self.reports, [("10.0.0.5", 2400, 1080, False)])

    def test_garbage_is_logged_not_passed_on(self):
        self.b._clients = [self.phone]
        with mock.patch.object(tandemd, "log") as log:
            self.send(b"garbage")
        self.assertEqual(self.reports, [])
        self.assertIn("unreadable screen size", log.call_args[0][0])

    def test_without_fit_to_device_it_is_ignored(self):
        self.b.on_device_size = None
        self.b._clients = [self.phone]
        self.send(b'{"width":2400,"height":1080}')     # no error


def daemon_args(**changes):
    args = types.SimpleNamespace(side="right", width=1920, height=1080, fps=60,
                                 scale=1.0, quality="high", bitrate="12M",
                                 codec="h264", gop=30, banner_file=None)
    for key, value in changes.items():
        setattr(args, key, value)
    return args


class ResizeMonitorTest(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(tandemd, "log")
        self.log = patcher.start()
        self.addCleanup(patcher.stop)
        self.args = daemon_args()
        self.backend = mock.Mock()
        self.backend.name = "kde"
        self.broadcaster = mock.Mock()
        self.saved = []

    def resize(self, size):
        return tandemd.resize_monitor(self.backend, self.args, size,
                                      self.broadcaster, save=self.saved.append)

    def test_the_monitor_phones_and_state_get_the_new_size(self):
        self.assertTrue(self.resize((2400, 1080)))
        self.backend.resize.assert_called_once_with(2400, 1080)
        self.assertEqual((self.args.width, self.args.height), (2400, 1080))
        banner = json.loads(self.broadcaster.set_banner.call_args[0][0])
        self.assertEqual((banner["width"], banner["height"]), (2400, 1080))
        self.assertEqual(self.saved, [(2400, 1080)])

    def test_a_failed_resize_puts_the_old_size_back(self):
        self.backend.resize.side_effect = [RuntimeError("refused"), None]
        self.assertFalse(self.resize((2400, 1080)))
        self.assertEqual(self.backend.resize.call_args_list,
                         [mock.call(2400, 1080), mock.call(1920, 1080)])
        self.assertEqual((self.args.width, self.args.height), (1920, 1080))
        self.broadcaster.set_banner.assert_not_called()
        self.assertEqual(self.saved, [])

    def test_no_monitor_at_all_is_an_error(self):
        self.backend.resize.side_effect = RuntimeError("gone")
        with self.assertRaises(RuntimeError):
            self.resize((2400, 1080))

    def test_the_banner_file_size_gives_way_to_the_fitted_one(self):
        path = os.path.join(support.SCRATCH_HOME, "banner-test.json")
        self.addCleanup(os.remove, path)
        with open(path, "w") as f:
            json.dump({"side": "left", "width": 1920, "height": 1080}, f)
        args = daemon_args(banner_file=path, width=2400)
        banner = json.loads(tandemd.load_banner_payload(args, "kde"))
        self.assertEqual((banner["side"], banner["width"]), ("left", 2400))


class SupervisorBetweenRunsTest(unittest.TestCase):
    """supervise_capture resizes with the encoder stopped."""

    def setUp(self):
        patcher = mock.patch.object(tandemd, "log")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stop = threading.Event()
        self.restart = threading.Event()
        self.order = []
        self.backend = mock.Mock()
        self.backend.stop_capture.side_effect = lambda: self.order.append("stop")

        def start():
            self.order.append("start")
            self.stop.set()                 # one restart is enough
            return mock.Mock()
        self.backend.start_capture.side_effect = start
        self.parse = mock.patch.object(tandemd, "parse_annexb",
                                       return_value="stopped")
        self.parse.start()
        self.addCleanup(self.parse.stop)

    def test_resize_runs_between_stop_and_start(self):
        ok = tandemd.supervise_capture(self.backend, mock.Mock(), mock.Mock(),
                                       self.stop, self.restart,
                                       lambda: self.order.append("resize"))
        self.assertTrue(ok)
        self.assertEqual(self.order, ["stop", "resize", "start"])

    def test_a_lost_monitor_stops_the_daemon(self):
        def lose():
            raise RuntimeError("no monitor")
        ok = tandemd.supervise_capture(self.backend, mock.Mock(), mock.Mock(),
                                       self.stop, self.restart, lose)
        self.assertFalse(ok)
        self.assertTrue(self.stop.is_set())
        self.backend.start_capture.assert_not_called()


class HyprlandResizeTest(unittest.TestCase):
    def setUp(self):
        args = daemon_args(output="EXT-1")
        with mock.patch.object(hyprland, "EventWatcher"):
            self.backend = hyprland.HyprlandBackend(args)
        self.backend._touch_mapped = True

    def test_the_output_gets_the_new_mode_and_touch_is_remapped(self):
        with mock.patch.object(hyprland, "place_output",
                               return_value=True) as place, \
             mock.patch.object(hyprland, "map_touch_device") as remap:
            self.backend.resize(2400, 1080)
        place.assert_called_once_with("EXT-1", 2400, 1080, 60, 1.0, "right")
        remap.assert_called_once()

    def test_a_refused_mode_raises(self):
        with mock.patch.object(hyprland, "place_output", return_value=False), \
             self.assertRaises(RuntimeError):
            self.backend.resize(2400, 1080)


class GnomeResizeTest(unittest.TestCase):
    def test_a_new_monitor_and_touch_follows_it(self):
        backend = gnome.GnomeBackend(daemon_args(touch=True))
        backend.connector = "Meta-0"
        backend._touch = mock.Mock()

        def setup():
            backend.remote_desktop, backend.stream = "/rd/2", "/stream/2"
        with mock.patch.object(backend, "teardown") as teardown, \
             mock.patch.object(backend, "setup", side_effect=setup):
            backend.resize(2400, 1080)
        teardown.assert_called_once()
        self.assertIsNone(backend.connector)        # found anew on placing
        backend._touch.retarget.assert_called_once_with("/rd/2", "/stream/2",
                                                        2400, 1080)

    def test_mutter_touch_switches_session_and_size(self):
        surface = gnome.MutterTouchscreen(mock.Mock(), "/rd/1", "/stream/1",
                                          1920, 1080)
        self.addCleanup(surface._worker.stop)
        surface.down(0, 0.5, 0.5)
        surface.retarget("/rd/2", "/stream/2", 2400, 1080)
        self.assertTrue(surface._worker.drain(1.0))
        self.assertEqual((surface._session, surface._stream),
                         ("/rd/2", "/stream/2"))
        self.assertEqual(surface._to_pixels(1.0, 1.0), (2399, 1079))
        self.assertEqual(surface._active_slots, set())


class KdeResizeTest(unittest.TestCase):
    def test_a_new_output_and_touch_follows_its_helper(self):
        backend = kde.KdeBackend(daemon_args())
        backend._touch = mock.Mock()
        new_helper = mock.Mock(fake_input_version=4)

        def setup():
            backend.helper = new_helper
        with mock.patch.object(backend, "teardown") as teardown, \
             mock.patch.object(backend, "setup", side_effect=setup):
            backend.resize(2400, 1080)
        teardown.assert_called_once()
        backend._touch.retarget.assert_called_once_with(new_helper)

    def test_kwin_touch_goes_to_the_new_helper(self):
        old, new = mock.Mock(output_name="Virtual-Tandem"), \
            mock.Mock(output_name="Virtual-Tandem")
        surface = kde.KwinTouchscreen(old)
        surface.down(0, 0.5, 0.5)
        surface.retarget(new)
        surface.move(0, 0.6, 0.6)                   # finger of the old output
        surface.down(1, 0.1, 0.1)
        new.send.assert_called_once()
        self.assertTrue(new.send.call_args[0][0].startswith("down 1 "))


class FitSettingTest(unittest.TestCase):
    def test_on_by_default_and_passed_to_the_daemon(self):
        cfg = core.Config(backend="kde")
        self.assertTrue(cfg.fit_device)
        self.assertIn("--fit-device", cfg.tandemd_args())

    def test_a_fixed_resolution_turns_it_off(self):
        cfg = core.Config(backend="kde")
        index = next(i for i, (_label, dims) in enumerate(core.RES_PRESETS)
                     if dims == (2560, 1440))
        tui._apply_resolution(cfg, index)
        self.assertFalse(cfg.fit_device)
        self.assertEqual((cfg.width, cfg.height), (2560, 1440))
        self.assertNotIn("--fit-device", cfg.tandemd_args())
        tui._apply_resolution(cfg, 0)
        self.assertTrue(cfg.fit_device)

    def test_changing_it_restarts_the_stream(self):
        self.assertIn("fit_device", tui.RESTART_KEYS)

    def test_how_it_reads(self):
        cfg = core.Config(backend="kde")
        self.assertEqual(tui.resolution_text(cfg), "fit to device")
        self.assertEqual(tui.resolution_text(cfg, (2400, 1080)),
                         "2400x1080 (fit to device)")
        cfg.fit_device = False
        self.assertEqual(tui.resolution_text(cfg, (2400, 1080)), "1920x1080")

    def test_the_live_view_learns_the_fitted_size_from_the_log(self):
        stream = tui.Stream(core.Config(backend="kde"))
        stream.proc = types.SimpleNamespace(stderr=iter([
            "[tandemd] fit to device: starting at the last device's size "
            "2400x1080\n",
            "[tandemd] virtual monitor is now 2560x1600\n"]))
        stream._reader()
        self.assertEqual(stream.monitor_size, (2560, 1600))


if __name__ == "__main__":
    unittest.main()
