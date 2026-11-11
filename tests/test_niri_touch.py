"""Tests for the niri backend's touch plumbing: where tandem.kdl lives,
whether niri's config includes it (with niri's include syntax and path
rules), writing and clearing the mapping, the one-time setup fix, and
spotting a real touchscreen in a fake sysfs tree."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import os
import shutil
import socket
import subprocess
import tempfile
import types
import unittest
from unittest import mock

from backends import niri, system


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tandem-niri-touch-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)

    def write(self, relative, text):
        path = os.path.join(self.root, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w") as f:
            f.write(text)
        return path

    def environment(self, **values):
        patch = mock.patch.dict(os.environ, values)
        patch.start()
        self.addCleanup(patch.stop)


class ConfigLocationTest(TempDirTestCase):
    def test_default_location(self):
        self.environment(XDG_CONFIG_HOME=os.path.join(self.root, "cfg"),
                         NIRI_CONFIG="")
        niri_dir = os.path.join(self.root, "cfg", "niri")
        self.assertEqual(niri.config_dir(), niri_dir)
        self.assertEqual(niri.user_config_file(),
                         os.path.join(niri_dir, "config.kdl"))
        self.assertEqual(niri.touch_file_path(),
                         os.path.join(niri_dir, "tandem.kdl"))

    def test_niri_config_variable_without_a_reachable_niri(self):
        path = os.path.join(self.root, "dots", "niri.kdl")
        self.environment(NIRI_CONFIG=path)
        self.assertEqual(niri.user_config_file(), path)
        self.assertEqual(niri.touch_file_path(),
                         os.path.join(self.root, "dots", "tandem.kdl"))
        self.assertEqual(niri.active_config_file(), path)

    def test_system_config_when_the_user_has_none(self):
        self.environment(XDG_CONFIG_HOME=os.path.join(self.root, "cfg"),
                         NIRI_CONFIG="")
        system = self.write("etc/niri/config.kdl", "")
        with mock.patch.object(niri, "SYSTEM_CONFIG", system):
            self.assertEqual(niri.active_config_file(), system)
        with mock.patch.object(niri, "SYSTEM_CONFIG",
                               os.path.join(self.root, "none.kdl")):
            self.assertIsNone(niri.active_config_file())


class RunningNiriConfigTest(TempDirTestCase):
    """niri removes NIRI_CONFIG before starting clients and Tandem never
    sees `niri -c`, so the config path comes from niri's own process."""

    def fake_proc(self, pid, argv, environment, cwd=None):
        base = os.path.join(self.root, "proc", str(pid))
        os.makedirs(base)
        with open(os.path.join(base, "cmdline"), "wb") as f:
            f.write(b"\0".join(arg.encode() for arg in argv) + b"\0")
        with open(os.path.join(base, "environ"), "wb") as f:
            f.write(b"\0".join(f"{name}={value}".encode()
                                for name, value in environment.items())
                    + b"\0")
        if cwd:
            os.symlink(cwd, os.path.join(base, "cwd"))
        return os.path.join(self.root, "proc")

    def test_config_argument_forms(self):
        cases = {
            ("niri", "--session"): None,
            ("niri", "-c", "/a.kdl"): "/a.kdl",
            ("niri", "--config", "/b.kdl", "--session"): "/b.kdl",
            ("niri", "--config=/c.kdl"): "/c.kdl",
            ("niri", "-c/d.kdl"): "/d.kdl",
            ("niri", "-c=/e.kdl"): "/e.kdl",
            ("niri", "--", "foot", "-c", "/f.kdl"): None,
            ("niri", "-c"): None,
        }
        for argv, expected in cases.items():
            with self.subTest(argv=argv):
                self.assertEqual(niri.config_argument(list(argv)), expected)

    def test_location_rules(self):
        home = {"HOME": "/home/u"}
        self.assertEqual(niri.config_location_from(["niri"], home, "/"),
                         ("/home/u/.config/niri/config.kdl", False))
        self.assertEqual(niri.config_location_from(
            ["niri"], {**home, "XDG_CONFIG_HOME": "/x"}, "/"),
            ("/x/niri/config.kdl", False))
        self.assertEqual(niri.config_location_from(
            ["niri"], {**home, "XDG_CONFIG_HOME": "rel"}, "/"),
            ("/home/u/.config/niri/config.kdl", False))
        self.assertEqual(niri.config_location_from(
            ["niri"], {**home, "NIRI_CONFIG": "/dots/n.kdl"}, "/"),
            ("/dots/n.kdl", True))
        # -c beats NIRI_CONFIG, and a relative path is niri's cwd's.
        self.assertEqual(niri.config_location_from(
            ["niri", "-c", "n.kdl"], {**home, "NIRI_CONFIG": "/x.kdl"},
            "/home/u/dots"), ("/home/u/dots/n.kdl", True))

    def test_the_running_niris_environment_wins_over_ours(self):
        # Tandem's environment has no NIRI_CONFIG (niri removed it before
        # starting the terminal); niri's start-up environment still has it.
        custom = self.write("dots/niri.kdl", "")
        proc = self.fake_proc(4242, ["niri", "--session"],
                              {"HOME": self.root, "NIRI_CONFIG": custom},
                              cwd=self.root)
        self.environment(XDG_CONFIG_HOME=os.path.join(self.root, "cfg"),
                         NIRI_CONFIG="")
        launch = system.read_process_launch(4242, proc_root=proc)
        self.assertEqual(launch.cwd, self.root)
        with mock.patch.object(niri, "running_niri_pid", return_value=4242), \
                mock.patch.object(niri, "read_process_launch",
                                  return_value=launch):
            self.assertEqual(niri.user_config_file(), custom)
            self.assertEqual(niri.active_config_file(), custom)
            self.assertEqual(niri.touch_file_path(),
                             os.path.join(self.root, "dots", "tandem.kdl"))
            _problem, fix = niri.include_fix()
            subprocess.run(fix, check=True)
            self.assertTrue(niri.config_includes_touch_file())
        self.assertFalse(os.path.exists(os.path.join(self.root, "cfg")))

    def test_unreadable_proc_entries(self):
        self.assertIsNone(system.read_process_launch(
            1, proc_root=os.path.join(self.root, "nothing")))
        proc = self.fake_proc(7, [], {})
        self.assertIsNone(system.read_process_launch(7, proc_root=proc))

    def test_running_niri_pid_is_the_socket_owner(self):
        path = os.path.join(self.root, "niri.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(1)
        self.assertEqual(niri.running_niri_pid(path), os.getpid())
        self.assertIsNone(niri.running_niri_pid(
            os.path.join(self.root, "missing.sock")))
        with mock.patch.dict(os.environ, {"NIRI_SOCKET": ""}):
            self.assertIsNone(niri.running_niri_pid())


class IncludeDetectionTest(TempDirTestCase):
    def includes(self, text, extra_files=()):
        config = self.write("niri/config.kdl", text)
        for relative, content in extra_files:
            self.write(relative, content)
        return niri.config_includes_touch_file(
            config, os.path.join(self.root, "niri", "tandem.kdl"))

    def test_the_line_the_fix_writes(self):
        self.assertTrue(self.includes(
            'input {\n    keyboard { }\n}\n\n'
            'include optional=true "tandem.kdl"\n'))

    def test_spellings_niri_accepts(self):
        for line in ('include "tandem.kdl"',
                     'include "./tandem.kdl"',
                     'include optional=false "tandem.kdl"',
                     'include "tandem.kdl" optional=true',
                     '   include   optional=true   "tandem.kdl" ;',
                     f'include "{self.root}/niri/tandem.kdl"',
                     'include r"tandem.kdl"',
                     'include r#"tandem.kdl"#'):
            with self.subTest(line=line):
                self.assertTrue(self.includes(line + "\n"))

    def test_home_relative_path(self):
        self.environment(HOME=self.root)
        self.assertTrue(self.includes('include "~/niri/tandem.kdl"\n'))

    def test_commented_out_lines_do_not_count(self):
        for text in ('// include "tandem.kdl"\n',
                     '/- include "tandem.kdl"\n',
                     '/* include "tandem.kdl" */\n',
                     '/*\n  /* nested */\n include "tandem.kdl"\n*/\n',
                     'binds { Mod+T { spawn "sh" "-c" '
                     '"include \\"tandem.kdl\\""; } }\n'):
            with self.subTest(text=text):
                self.assertFalse(self.includes(text))

    def test_other_files_and_directories_do_not_count(self):
        self.assertFalse(self.includes('include "colors.kdl"\n'))
        self.assertFalse(self.includes('include "sub/tandem.kdl"\n'))
        self.assertFalse(self.includes('include "tandem.kdl.bak"\n'))

    def test_nested_include(self):
        self.assertTrue(self.includes(
            'include "parts/main.kdl"\n',
            [("niri/parts/main.kdl", 'include "../tandem.kdl"\n')]))
        # Relative to the including file, not to config.kdl:
        self.assertFalse(self.includes(
            'include "parts/main.kdl"\n',
            [("niri/parts/main.kdl", 'include "tandem.kdl"\n')]))

    def test_recursive_includes_terminate(self):
        self.assertFalse(self.includes(
            'include "a.kdl"\n',
            [("niri/a.kdl", 'include "config.kdl"\n')]))

    def test_missing_config(self):
        self.assertFalse(niri.config_includes_touch_file(
            os.path.join(self.root, "nope.kdl"),
            os.path.join(self.root, "tandem.kdl")))
        with mock.patch.object(niri, "active_config_file",
                               return_value=None):
            self.assertFalse(niri.config_includes_touch_file())


class IncludeFixTest(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.environment(XDG_CONFIG_HOME=os.path.join(self.root, "cfg"),
                         NIRI_CONFIG="")
        self.user = os.path.join(self.root, "cfg", "niri", "config.kdl")

    def test_appends_the_line_and_is_then_detected(self):
        self.write("cfg/niri/config.kdl", "layout { gaps 8; }")  # no newline
        problem, fix = niri.include_fix()
        self.assertIn(niri.INCLUDE_LINE, problem)
        self.assertNotIn("sudo", fix)
        subprocess.run(fix, check=True)
        with open(self.user) as f:
            self.assertEqual(f.read(), "layout { gaps 8; }\n"
                             'include optional=true "tandem.kdl"\n')
        self.assertTrue(niri.config_includes_touch_file())

    def test_copies_the_system_config_first(self):
        system = self.write("etc/niri/config.kdl", "// system defaults\n")
        with mock.patch.object(niri, "SYSTEM_CONFIG", system):
            _problem, fix = niri.include_fix()
            subprocess.run(fix, check=True)
            with open(self.user) as f:
                self.assertEqual(f.read(), "// system defaults\n\n"
                                 + niri.INCLUDE_LINE + "\n")
            self.assertTrue(niri.config_includes_touch_file())

    def test_no_config_at_all_has_no_fix(self):
        with mock.patch.object(niri, "SYSTEM_CONFIG",
                               os.path.join(self.root, "none.kdl")):
            problem, fix = niri.include_fix()
        self.assertIsNone(fix)
        self.assertIn("doesn't exist yet", problem)

    def test_a_missing_explicit_config_is_not_replaced_by_the_system_one(self):
        # niri -c / NIRI_CONFIG naming a missing file runs on niri's
        # built-in defaults (niri-config ConfigPath::Explicit has no
        # fallback), so /etc/niri/config.kdl must not be copied there.
        explicit = os.path.join(self.root, "dots", "niri.kdl")
        self.environment(NIRI_CONFIG=explicit)
        system = self.write("etc/niri/config.kdl", "// system defaults\n")
        with mock.patch.object(niri, "SYSTEM_CONFIG", system):
            problem, fix = niri.include_fix()
        self.assertIsNone(fix)
        self.assertIn(explicit, problem)
        self.assertIn("doesn't exist", problem)
        self.assertFalse(os.path.exists(explicit))

    def test_paths_with_spaces_and_quotes_are_quoted(self):
        self.environment(NIRI_CONFIG=os.path.join(self.root, "my 'cfg'",
                                                  "config.kdl"))
        self.write("my 'cfg'/config.kdl", "")
        _problem, fix = niri.include_fix()
        subprocess.run(fix, check=True)
        self.assertTrue(niri.config_includes_touch_file())


class TouchFileTest(TempDirTestCase):
    def test_write_then_clear(self):
        path = os.path.join(self.root, "tandem.kdl")
        niri.write_touch_mapping(path)
        with open(path) as f:
            mapped = f.read()
        self.assertIn('map-to-output "Tandem"', mapped)
        self.assertTrue(mapped.startswith("// Managed by Tandem"))
        self.assertEqual(oct(os.stat(path).st_mode & 0o777), "0o644")
        self.assertTrue(niri.clear_touch_mapping(path))
        with open(path) as f:
            cleared = f.read()
        self.assertNotIn("map-to-output", cleared)
        self.assertNotIn("input", cleared.replace("// ", "").split("\n")[-1])
        self.assertFalse(niri.clear_touch_mapping(path))   # already clear
        self.assertEqual(os.listdir(self.root), ["tandem.kdl"])  # no temps

    def test_clear_leaves_a_missing_file_missing(self):
        path = os.path.join(self.root, "tandem.kdl")
        self.assertFalse(niri.clear_touch_mapping(path))
        self.assertFalse(os.path.exists(path))

    def test_clear_never_raises(self):
        path = self.write("ro/tandem.kdl", niri.TOUCH_FILE_MAPPED)
        with mock.patch.object(niri, "write_atomically",
                               side_effect=PermissionError("read-only")), \
                mock.patch.object(niri, "log") as log:
            self.assertFalse(niri.clear_touch_mapping(path))
        self.assertIn("could not clear", log.call_args[0][0])

    def test_kdl_is_what_niri_parses(self):
        # Every non-comment line of the mapping, exactly; niri's own parse
        # of this text is checked in the Docker end-to-end run.
        body = [line for line in niri.TOUCH_FILE_MAPPED.splitlines()
                if not line.startswith("//")]
        self.assertEqual(body, ["input {", "    touch {",
                                '        map-to-output "Tandem"', "    }",
                                "}"])
        self.assertTrue(all(line.startswith("//")
                            for line in niri.TOUCH_FILE_CLEARED.splitlines()))


def fake_input_device(root, event, name, properties, absolute):
    device = os.path.join(root, event, "device")
    os.makedirs(os.path.join(device, "capabilities"))
    for relative, text in (("name", name), ("properties", properties),
                           ("capabilities/abs", absolute)):
        with open(os.path.join(device, relative), "w") as f:
            f.write(text + "\n")


# Real sysfs values: a laptop touchscreen (ELAN), a touchpad (pointer, not
# direct), a keyboard, a Wacom pen display (direct, but no multi-touch) and
# Tandem's own uinput device.
# 64-bit kernel words: ABS_X, ABS_Y and ABS_MT_SLOT / POSITION_X / _Y /
# TRACKING_ID (bits 0, 1, 47, 53, 54, 57).
TOUCHSCREEN_ABS = "260800000000003"
TOUCHPAD_ABS = "260800000000003"


class TouchscreenDetectionTest(TempDirTestCase):
    def test_parse_bitmap(self):
        self.assertEqual(niri.parse_bitmap("3", 64), 3)
        self.assertEqual(niri.parse_bitmap("1 0", 64), 1 << 64)
        self.assertEqual(niri.parse_bitmap("1 0", 32), 1 << 32)
        self.assertEqual(niri.parse_bitmap("0"), 0)
        with self.assertRaises(ValueError):
            niri.parse_bitmap("zz")

    def test_finds_only_real_touchscreens(self):
        fake_input_device(self.root, "event3", "ELAN2514:00 04F3:2A9C",
                          "2", TOUCHSCREEN_ABS)
        fake_input_device(self.root, "event5", "SYNA2B61:00 06CB:CE78 "
                          "Touchpad", "5", TOUCHPAD_ABS)
        fake_input_device(self.root, "event1", "AT Translated Set 2 "
                          "keyboard", "0", "0")
        fake_input_device(self.root, "event7", "Wacom Cintiq Pen", "2",
                          "1000003")
        fake_input_device(self.root, "event9", "tandem-touch", "2",
                          TOUCHSCREEN_ABS)
        os.makedirs(os.path.join(self.root, "mouse0"))
        os.makedirs(os.path.join(self.root, "event11"))   # no device dir
        self.assertEqual(niri.real_touchscreens(self.root),
                         ["ELAN2514:00 04F3:2A9C"])

    def test_no_touchscreen(self):
        fake_input_device(self.root, "event5", "Touchpad", "5", TOUCHPAD_ABS)
        self.assertEqual(niri.real_touchscreens(self.root), [])
        self.assertEqual(niri.real_touchscreens(
            os.path.join(self.root, "missing")), [])

    def test_multi_word_bitmaps(self):
        # ABS_MT_POSITION_X is bit 53; on a 32-bit kernel it lands in the
        # second word.
        fake_input_device(self.root, "event2", "Goodix touch", "2",
                          "260800 3")
        with mock.patch.object(niri, "KERNEL_LONG_BITS", 32):
            self.assertEqual(niri.real_touchscreens(self.root),
                             ["Goodix touch"])


class TouchSetupProblemTest(unittest.TestCase):
    UINPUT = {"problem": "no permission to use /dev/uinput",
              "fix": ["sudo", "sh", "-c", "..."]}

    def patches(self, uinput=None, touchscreens=(), included=True):
        return [mock.patch.object(niri, "uinput_setup_problem",
                                  return_value=uinput),
                mock.patch.object(niri, "real_touchscreens",
                                  return_value=list(touchscreens)),
                mock.patch.object(niri, "config_includes_touch_file",
                                  return_value=included),
                mock.patch.object(niri, "include_fix",
                                  return_value=("needs the include",
                                                ["sh", "-c", "append"]))]

    def problem(self, **state):
        patches = self.patches(**state)
        for patch in patches:
            patch.start()
        try:
            return niri.touch_setup_problem()
        finally:
            for patch in patches:
                patch.stop()

    def test_uinput_comes_first(self):
        self.assertEqual(self.problem(uinput=self.UINPUT, included=False),
                         self.UINPUT)

    def test_then_the_include(self):
        self.assertEqual(self.problem(included=False),
                         {"problem": "needs the include",
                          "fix": ["sh", "-c", "append"]})

    def test_set_up(self):
        self.assertIsNone(self.problem())

    def test_a_touchscreen_is_not_a_setup_problem(self):
        self.assertIsNone(self.problem(touchscreens=["ELAN"],
                                       included=False))

    def test_never_raises(self):
        with mock.patch.object(niri, "uinput_setup_problem",
                               side_effect=OSError("boom")):
            self.assertIsNone(niri.touch_setup_problem())


class CreateTouchSurfaceTest(TempDirTestCase):
    def backend(self):
        backend = niri.NiriBackend(types.SimpleNamespace(
            width=2400, height=1080, fps=60, scale=1.5, side="right"))
        backend._watcher = mock.Mock()
        return backend

    def setUp(self):
        super().setUp()
        self.environment(XDG_CONFIG_HOME=self.root, NIRI_CONFIG="")
        self.touch_file = os.path.join(self.root, "niri", "tandem.kdl")
        os.makedirs(os.path.dirname(self.touch_file))

    def surface(self, uinput=None, touchscreens=(), included=True,
                device=None):
        device = device or mock.Mock(description="uinput touchscreen "
                                                 "(10 fingers)")
        with mock.patch.object(niri.uinput_touch, "uinput_access_problem",
                               return_value=uinput), \
                mock.patch.object(niri, "real_touchscreens",
                                  return_value=list(touchscreens)), \
                mock.patch.object(niri, "config_includes_touch_file",
                                  return_value=included), \
                mock.patch.object(niri.uinput_touch, "VirtualTouchscreen",
                                  return_value=device) as create:
            backend = self.backend()
            return backend, backend.create_touch_surface(), create

    def test_maps_and_creates_the_device(self):
        backend, screen, create = self.surface()
        create.assert_called_once_with(2400, 1080)
        self.assertIn("mapped to Tandem", screen.description)
        with open(self.touch_file) as f:
            self.assertIn('map-to-output "Tandem"', f.read())
        self.assertTrue(backend._touch_mapped)

    def test_errors_are_for_the_user(self):
        cases = ((dict(uinput="no permission to use /dev/uinput"),
                  "press t"),
                 (dict(touchscreens=["ELAN"]), "touchscreen"),
                 (dict(included=False), "press t"))
        for state, text in cases:
            with self.subTest(state=state):
                with self.assertRaisesRegex(RuntimeError, text):
                    self.surface(**state)
                self.assertFalse(os.path.exists(self.touch_file))

    def test_mapping_is_cleared_when_the_device_fails(self):
        with mock.patch.object(niri.uinput_touch, "uinput_access_problem",
                               return_value=None), \
                mock.patch.object(niri, "real_touchscreens", return_value=[]), \
                mock.patch.object(niri, "config_includes_touch_file",
                                  return_value=True), \
                mock.patch.object(niri.uinput_touch, "VirtualTouchscreen",
                                  side_effect=OSError("EBUSY")):
            backend = self.backend()
            with self.assertRaises(OSError):
                backend.create_touch_surface()
        with open(self.touch_file) as f:
            self.assertNotIn("map-to-output", f.read())
        self.assertFalse(backend._touch_mapped)


if __name__ == "__main__":
    unittest.main()
