"""Tests for the per-desktop parts of core.py: the dependency rows and the
functions that ask the backend's module hooks (touch setup, leftover output
removal, primary monitor line)."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import unittest
from unittest import mock

import core
import uinput_touch
from backends import gnome, hyprland

GNOME_LABELS = ["gst-launch-1.0", "python gi", "pipewiresrc", "videoconvert",
                "h264parse", "H.264 encoder"]


def config(backend, **fields):
    return core.Config(backend=backend, **fields)


class DepsTest(unittest.TestCase):
    def test_gstreamer_pipeline_rows(self):
        self.assertEqual([row[0] for row in core._GSTREAMER_PIPELINE_ROWS],
                         ["gst-launch-1.0", "pipewiresrc", "videoconvert",
                          "h264parse", "H.264 encoder"])
        self.assertTrue(all(row[3] for row in core._GSTREAMER_PIPELINE_ROWS))

    def test_gnome_rows_are_unchanged(self):
        rows = core.DEPS["gnome"]
        self.assertEqual([row[0] for row in rows], GNOME_LABELS)
        packages = {row[0]: row[2] for row in rows}
        self.assertEqual(packages["python gi"],
                         {"arch": "python-gobject", "debian": "python3-gi",
                          "fedora": "python3-gobject"})
        self.assertEqual(packages["H.264 encoder"]["fedora"],
                         "gstreamer1-plugin-openh264")
        self.assertEqual(rows[1][4], "talks to Mutter over D-Bus")
        self.assertTrue(all(row[3] for row in rows))

    def test_rows_have_the_documented_shape(self):
        for backend, rows in core.DEPS.items():
            for label, check, packages, required, purpose in rows:
                with self.subTest(backend=backend, label=label):
                    self.assertTrue(callable(check))
                    self.assertIn("arch", packages)   # unknown distro falls back
                    self.assertIsInstance(required, bool)
                    self.assertTrue(purpose)

    def test_check_dependencies_lists_backend_rows_then_common(self):
        with mock.patch.object(core, "distro", return_value="debian"):
            rows = core.check_dependencies("gnome")
        self.assertEqual([row[0] for row in rows],
                         GNOME_LABELS + ["openssl", "adb"])
        self.assertEqual(rows[0][1], "gstreamer1.0-tools")

    def test_unknown_backend_has_only_common_rows(self):
        with mock.patch.object(core, "distro", return_value="arch"):
            self.assertEqual([row[0] for row in core.check_dependencies(None)],
                             ["openssl", "adb"])


class TouchSetupCheckTest(unittest.TestCase):
    PROBLEM = "no permission to use /dev/uinput"

    def check(self, backend, problem, touch=True):
        with mock.patch.object(uinput_touch, "uinput_access_problem",
                               return_value=problem):
            return core.touch_setup_check(config(backend, touch=touch))

    def test_hyprland_needs_the_udev_rule(self):
        result = self.check("hyprland", self.PROBLEM)
        self.assertEqual(result["problem"], self.PROBLEM)
        self.assertEqual(result["fix"][:3], ["sudo", "sh", "-c"])
        script = result["fix"][3]
        self.assertIn(hyprland.UINPUT_RULE_PATH, script)
        self.assertIn(hyprland.UINPUT_MODULE_PATH, script)
        self.assertIn("'" + hyprland.UINPUT_RULE + "'", script)
        self.assertTrue(script.endswith("udevadm settle"))

    def test_nothing_to_do(self):
        self.assertIsNone(self.check("hyprland", None))
        self.assertIsNone(self.check("hyprland", self.PROBLEM, touch=False))
        self.assertIsNone(self.check("gnome", self.PROBLEM))
        self.assertIsNone(self.check("sway", self.PROBLEM))

    def test_a_failing_hook_is_not_an_error(self):
        with mock.patch.object(hyprland, "touch_setup_problem",
                               side_effect=OSError("boom")):
            self.assertIsNone(core.touch_setup_check(config("hyprland")))


class RemoveVirtualOutputTest(unittest.TestCase):
    def test_hyprland_removes_the_configured_output(self):
        with mock.patch.object(hyprland, "remove_output") as remove_output:
            core.remove_virtual_output(config("hyprland", output="EXT-3"))
        remove_output.assert_called_once_with("EXT-3")

    def test_errors_are_swallowed(self):
        with mock.patch.object(hyprland, "remove_output",
                               side_effect=RuntimeError("hyprctl failed")):
            core.remove_virtual_output(config("hyprland"))

    def test_other_desktops_do_nothing(self):
        with mock.patch.object(hyprland, "remove_output") as remove_output:
            core.remove_virtual_output(config("gnome"))
            core.remove_virtual_output(config("sway"))
        remove_output.assert_not_called()


class PrimaryMonitorDescTest(unittest.TestCase):
    def test_hyprland(self):
        monitor = {"name": "DP-1", "width": 2560, "height": 1440,
                   "refreshRate": 143.9, "scale": 1.25}
        with mock.patch.object(hyprland, "primary_for",
                               return_value=monitor) as primary_for:
            self.assertEqual(core.primary_monitor_desc("hyprland"),
                             "DP-1  2560x1440@144  scale 1.25")
        primary_for.assert_called_once_with("EXT-1")
        with mock.patch.object(hyprland, "primary_for", return_value=None):
            self.assertEqual(core.primary_monitor_desc("hyprland"),
                             "(none found)")

    def test_gnome(self):
        logicals = [(0, 0, 1.0, 0, False, [("HDMI-1", "", "", "")], {}),
                    (1920, 0, 1.5, 0, True, [("eDP-1", "", "", "")], {})]
        fake_gio = mock.Mock()
        with mock.patch.object(gnome, "Gio", fake_gio), \
                mock.patch.object(gnome, "display_state",
                                  return_value=(1, [], logicals, {})):
            self.assertEqual(core.primary_monitor_desc("gnome"),
                             "eDP-1  at 1920,0  scale 1.5")
        fake_gio.bus_get_sync.side_effect = RuntimeError("no session bus")
        with mock.patch.object(gnome, "Gio", fake_gio):
            self.assertEqual(core.primary_monitor_desc("gnome"), "(none found)")
        with mock.patch.object(gnome, "Gio", None):
            self.assertEqual(core.primary_monitor_desc("gnome"), "(none found)")

    def test_unsupported(self):
        self.assertEqual(core.primary_monitor_desc(None), "(none found)")
        self.assertEqual(core.primary_monitor_desc("sway"), "(none found)")


if __name__ == "__main__":
    unittest.main()
