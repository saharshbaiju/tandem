"""Tests for backends/__init__.py: the registry, detection, the module hooks
every backend must define, and the class attributes tandemd relies on."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import contextlib
import io
import os
import sys
import unittest
from unittest import mock

import backends
from backends import gnome, hyprland

HOOKS = ("session_confidence", "session_problem", "describe_primary_monitor",
         "remove_leftover_output", "touch_setup_problem")


def detect_with(env):
    with mock.patch.dict(os.environ, env, clear=True):
        return backends.detect()


class RegistryTest(unittest.TestCase):
    def test_names_are_in_display_order(self):
        self.assertEqual(backends.NAMES[:2], ("hyprland", "gnome"))
        self.assertEqual(backends.NAMES, tuple(backends._REGISTRY))

    def test_every_backend_module_is_loaded_and_reachable(self):
        self.assertIs(backends.module("hyprland"), hyprland)
        self.assertIs(backends.module("gnome"), gnome)
        self.assertIs(backends.gnome, gnome)    # tui.py style access
        self.assertIsNone(backends.module("sway"))
        self.assertIsNone(backends.module(None))

    def test_every_backend_defines_the_hooks(self):
        for name in backends.NAMES:
            for hook in HOOKS:
                with self.subTest(backend=name, hook=hook):
                    self.assertTrue(callable(getattr(backends.module(name),
                                                     hook, None)))

    def test_every_backend_class_has_the_contract_attributes(self):
        for name in backends.NAMES:
            entry = backends._REGISTRY[name]
            cls = getattr(backends.module(name), entry.class_name)
            with self.subTest(backend=name):
                self.assertEqual(cls.name, name)
                self.assertIsInstance(cls.honors_keep_output, bool)
                for method in ("missing", "setup", "start_capture",
                               "stop_capture", "apply_live",
                               "create_touch_surface", "teardown"):
                    self.assertTrue(callable(getattr(cls, method, None)), method)

    def test_honors_keep_output(self):
        self.assertIs(hyprland.HyprlandBackend.honors_keep_output, True)
        self.assertIs(gnome.GnomeBackend.honors_keep_output, False)

    def test_create(self):
        args = mock.Mock()
        self.assertIsInstance(backends.create("hyprland", args),
                              hyprland.HyprlandBackend)
        self.assertIsInstance(backends.create("gnome", args),
                              gnome.GnomeBackend)
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME",
                                          "WAYLAND_DISPLAY": "wayland-0"},
                             clear=True):
            self.assertIsInstance(backends.create("auto", args),
                                  gnome.GnomeBackend)
            self.assertIsInstance(backends.create(None, args),
                                  gnome.GnomeBackend)

    def test_create_unsupported_names_every_desktop(self):
        for name in ("sway", "auto"):
            with self.subTest(name=name), \
                    mock.patch.dict(os.environ, {}, clear=True), \
                    self.assertRaises(SystemExit) as raised:
                backends.create(name, mock.Mock())
            message = str(raised.exception)
            self.assertTrue(message.startswith("unsupported desktop: run "))
            self.assertTrue(message.endswith(". Use --backend to force one."))
            for entry in backends._REGISTRY.values():
                self.assertIn(entry.desktop, message)

    def test_supported_desktops_text(self):
        with mock.patch.dict(backends._REGISTRY, {}, clear=True):
            backends._REGISTRY["a"] = backends.Entry("a", "A", "", "Alpha")
            self.assertEqual(backends.supported_desktops(), "Alpha")
            backends._REGISTRY["b"] = backends.Entry("b", "B", "", "Beta")
            self.assertEqual(backends.supported_desktops(), "Alpha, or Beta")
            backends._REGISTRY["c"] = backends.Entry("c", "C", "", "Gamma")
            self.assertEqual(backends.supported_desktops(),
                             "Alpha, Beta, or Gamma")

    def test_describe(self):
        self.assertEqual(backends.describe("hyprland"),
                         "Hyprland (headless output + wf-recorder / portal)")
        self.assertEqual(backends.describe("gnome"),
                         "GNOME / Mutter (virtual monitor + PipeWire)")
        self.assertEqual(backends.describe(None), "unsupported desktop")
        self.assertEqual(backends.describe("sway"), "unsupported desktop")


class DetectTest(unittest.TestCase):
    def test_hyprland_signature_wins(self):
        self.assertEqual(detect_with({"HYPRLAND_INSTANCE_SIGNATURE": "abc"}),
                         "hyprland")
        self.assertEqual(detect_with({"HYPRLAND_INSTANCE_SIGNATURE": "abc",
                                      "XDG_CURRENT_DESKTOP": "GNOME",
                                      "WAYLAND_DISPLAY": "wayland-0"}),
                         "hyprland")

    def test_gnome_wayland(self):
        for desktop in ("GNOME", "ubuntu:GNOME", "GNOME-Classic:GNOME"):
            with self.subTest(desktop=desktop):
                self.assertEqual(detect_with({"XDG_CURRENT_DESKTOP": desktop,
                                              "WAYLAND_DISPLAY": "wayland-0"}),
                                 "gnome")

    def test_gnome_x11_is_still_gnome(self):
        # setup() fails there, and session_problem() explains why.
        self.assertEqual(detect_with({"XDG_CURRENT_DESKTOP": "GNOME"}), "gnome")

    def test_kde_on_x11_is_kde_but_only_by_name(self):
        # Like GNOME on X11: confidence 1, so it is still picked when it is
        # the only desktop named, and a tie with GNOME keeps the old answer.
        self.assertEqual(detect_with({"XDG_CURRENT_DESKTOP": "KDE"}), "kde")
        self.assertEqual(detect_with({"XDG_CURRENT_DESKTOP": "KDE:GNOME"}),
                         "gnome")
        self.assertEqual(detect_with({"XDG_CURRENT_DESKTOP": "KDE",
                                      "WAYLAND_DISPLAY": "wayland-0"}), "kde")

    def test_desktop_name_fallbacks(self):
        self.assertEqual(detect_with({"XDG_CURRENT_DESKTOP": "Hyprland"}),
                         "hyprland")
        # Both named, neither confirmed: the earlier entry wins (as before).
        self.assertEqual(detect_with({"XDG_CURRENT_DESKTOP": "Hyprland:GNOME"}),
                         "hyprland")

    def test_unsupported(self):
        self.assertIsNone(detect_with({}))
        self.assertIsNone(detect_with({"XDG_CURRENT_DESKTOP": "sway",
                                       "WAYLAND_DISPLAY": "wayland-1"}))

    def test_a_failing_hook_counts_as_no(self):
        with mock.patch.object(hyprland, "session_confidence",
                               side_effect=RuntimeError("boom")):
            self.assertEqual(detect_with({"HYPRLAND_INSTANCE_SIGNATURE": "x",
                                          "XDG_CURRENT_DESKTOP": "GNOME",
                                          "WAYLAND_DISPLAY": "w"}), "gnome")


class SessionProblemTest(unittest.TestCase):
    def test_gnome_on_x11(self):
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME"},
                             clear=True):
            lines = gnome.session_problem()
        self.assertEqual(lines[0], "GNOME is running on X11 — virtual "
                                   "monitors need Wayland.")
        with mock.patch.dict(os.environ, {"XDG_CURRENT_DESKTOP": "GNOME",
                                          "WAYLAND_DISPLAY": "wayland-0"},
                             clear=True):
            self.assertIsNone(gnome.session_problem())

    def test_hyprland_has_none(self):
        self.assertIsNone(hyprland.session_problem())


class TandemdArgumentsTest(unittest.TestCase):
    def test_backend_choices_follow_the_registry(self):
        import tandemd
        with mock.patch.object(sys, "argv", ["tandemd", "--help"]), \
                contextlib.redirect_stdout(io.StringIO()) as help_text, \
                self.assertRaises(SystemExit):
            tandemd.main()
        choices = "{" + ",".join(("auto",) + backends.NAMES) + "}"
        self.assertIn(f"--backend {choices}", help_text.getvalue())

    def test_unknown_backend_is_rejected(self):
        import tandemd
        with mock.patch.object(sys, "argv", ["tandemd", "--backend", "sway"]), \
                contextlib.redirect_stderr(io.StringIO()), \
                self.assertRaises(SystemExit) as raised:
            tandemd.main()
        self.assertEqual(raised.exception.code, 2)


if __name__ == "__main__":
    unittest.main()
