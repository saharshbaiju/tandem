"""Tests for backends/kwin_helper.py: where the KWin helper is installed, the
hash-based rebuild decision, the build itself, and KWin's permission entry;
plus the KDE rows of core.DEPS."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import os
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

import core
from backends import kwin_helper


def build_tools_present():
    return not kwin_helper.build_problem()


class ScratchDataHomeCase(unittest.TestCase):
    """Each test gets its own XDG_DATA_HOME."""

    def setUp(self):
        self.data_home = tempfile.mkdtemp(prefix="tandem-kwin-data-")
        self.addCleanup(shutil.rmtree, self.data_home, ignore_errors=True)
        patcher = mock.patch.dict(os.environ,
                                  {"XDG_DATA_HOME": self.data_home})
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(kwin_helper, "log")
        patcher.start()
        self.addCleanup(patcher.stop)

    def fake_install(self, built_from):
        """An installed 'helper' plus the hash it claims to be built from."""
        os.makedirs(kwin_helper.install_dir(), exist_ok=True)
        with open(kwin_helper.helper_path(), "w") as binary:
            binary.write("#!/bin/sh\n")
        os.chmod(kwin_helper.helper_path(), 0o755)
        with open(os.path.join(kwin_helper.install_dir(), "source.sha256"),
                  "w") as stored:
            stored.write(built_from + "\n")


class PathsTest(ScratchDataHomeCase):
    def test_paths_follow_xdg_data_home(self):
        self.assertEqual(kwin_helper.helper_path(), os.path.join(
            self.data_home, "tandem", "kwin-helper", "tandem-kwin-helper"))
        self.assertEqual(kwin_helper.desktop_entry_path(), os.path.join(
            self.data_home, "applications", "tandem-kwin-helper.desktop"))

    def test_the_entry_goes_where_kwin_looks(self):
        # XDG_DATA_HOME set in a shell rc file: the terminal (and Tandem)
        # see it, the Plasma session KWin runs in doesn't.
        kwin_home = os.path.join(self.data_home, "kwin-home")
        launch = kwin_helper.system.ProcessLaunch(
            ["kwin_wayland_wrapper"], {"HOME": kwin_home}, "/")
        with mock.patch.object(kwin_helper.system, "socket_owner_launch",
                               return_value=launch) as owner, \
                mock.patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-7",
                                             "XDG_RUNTIME_DIR": "/run/u"}):
            entry = kwin_helper.desktop_entry_path()
            self.assertTrue(kwin_helper.ensure_desktop_entry("/opt/helper"))
        owner.assert_called_with("/run/u/wayland-7")
        self.assertEqual(entry, os.path.join(
            kwin_home, ".local", "share", "applications",
            "tandem-kwin-helper.desktop"))
        self.assertTrue(os.path.isfile(entry))
        # The helper itself stays in our data home: Exec names it exactly.
        self.assertTrue(kwin_helper.helper_path().startswith(
            os.path.join(self.data_home, "tandem")))

    def test_wayland_socket_path(self):
        cases = (({"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": "/r"},
                  "/r/wayland-1"),
                 ({"WAYLAND_DISPLAY": "/tmp/w", "XDG_RUNTIME_DIR": ""},
                  "/tmp/w"),
                 ({"WAYLAND_DISPLAY": "", "XDG_RUNTIME_DIR": "/r"}, None),
                 ({"WAYLAND_DISPLAY": "wayland-1", "XDG_RUNTIME_DIR": ""},
                  None))
        for environment, expected in cases:
            with self.subTest(environment=environment), \
                    mock.patch.dict(os.environ, environment):
                self.assertEqual(kwin_helper.wayland_socket_path(), expected)

    def test_relative_or_empty_xdg_data_home_is_ignored(self):
        for value in ("", "relative/dir"):
            with self.subTest(value=value), \
                    mock.patch.dict(os.environ, {"XDG_DATA_HOME": value}):
                self.assertEqual(kwin_helper.data_home(), os.path.join(
                    os.path.expanduser("~"), ".local", "share"))

    def test_sources_are_in_the_repository(self):
        for path in kwin_helper._source_files():
            with self.subTest(path=path):
                self.assertTrue(os.path.isfile(path))


class RebuildDecisionTest(ScratchDataHomeCase):
    def test_nothing_installed_is_not_current(self):
        self.assertFalse(kwin_helper.is_current())

    def test_matching_hash_is_current(self):
        self.fake_install(kwin_helper.source_hash())
        self.assertTrue(kwin_helper.is_current())

    def test_changed_sources_or_flags_need_a_rebuild(self):
        self.fake_install(kwin_helper.source_hash())
        with mock.patch.object(kwin_helper, "CFLAGS", ("-O0",)):
            self.assertFalse(kwin_helper.is_current())
        self.fake_install("0" * 64)
        self.assertFalse(kwin_helper.is_current())

    def test_missing_binary_is_not_current(self):
        self.fake_install(kwin_helper.source_hash())
        os.unlink(kwin_helper.helper_path())
        self.assertFalse(kwin_helper.is_current())

    def test_unreadable_sources_have_no_hash(self):
        with mock.patch.object(kwin_helper, "SOURCE_DIR", self.data_home):
            self.assertIsNone(kwin_helper.source_hash())
            self.assertIn(f"helper sources ({self.data_home})",
                          kwin_helper.build_problem())
            with self.assertRaises(RuntimeError):
                kwin_helper.ensure_built()

    def test_ensure_built_skips_a_current_helper(self):
        self.fake_install(kwin_helper.source_hash())
        with mock.patch.object(kwin_helper, "_compile_into") as compile_into:
            self.assertEqual(kwin_helper.ensure_built(),
                             kwin_helper.helper_path())
        compile_into.assert_not_called()

    def test_ensure_built_installs_and_records_the_hash(self):
        def fake_compile(work_dir):
            binary = os.path.join(work_dir, "tandem-kwin-helper")
            with open(binary, "w") as stream:
                stream.write("#!/bin/sh\n")
            return binary
        self.fake_install("0" * 64)
        with mock.patch.object(kwin_helper, "_compile_into",
                               side_effect=fake_compile) as compile_into:
            path = kwin_helper.ensure_built()
        compile_into.assert_called_once()
        self.assertTrue(os.access(path, os.X_OK))
        self.assertTrue(kwin_helper.is_current())
        self.assertFalse(os.path.exists(path + ".new"))

    def test_a_failed_build_keeps_the_old_binary_and_explains(self):
        self.fake_install("0" * 64)
        failure = subprocess.CompletedProcess(
            [], 1, "", "tandem-kwin-helper.c:1: error: boom\n")
        with mock.patch.object(kwin_helper.subprocess, "run",
                               return_value=failure), \
                mock.patch.object(kwin_helper.shutil, "which",
                                  return_value="/usr/bin/tool"):
            with self.assertRaises(RuntimeError) as raised:
                kwin_helper.ensure_built()
        self.assertIn("error: boom", str(raised.exception))
        self.assertTrue(os.path.exists(kwin_helper.helper_path()))
        self.assertFalse(kwin_helper.is_current())

    def test_build_problem_names_each_missing_tool(self):
        with mock.patch.object(kwin_helper.shutil, "which",
                               return_value=None):
            problem = kwin_helper.build_problem()
        self.assertEqual(problem, ["C compiler (cc)", "pkg-config",
                                   "wayland-scanner",
                                   "wayland-client development files"])

    @unittest.skipUnless(build_tools_present(),
                         "needs cc, pkg-config, wayland-scanner and "
                         "libwayland-client headers")
    def test_real_build(self):
        path = kwin_helper.ensure_built()
        self.assertTrue(kwin_helper.is_current())
        result = subprocess.run([path, "--help"], capture_output=True,
                                text=True, timeout=10)
        self.assertEqual(result.returncode, 0)
        self.assertIn("--name NAME --width W --height H", result.stdout)
        bad = subprocess.run([path, "--name", "bad name", "--width", "1",
                              "--height", "1"], capture_output=True,
                             text=True, timeout=10)
        self.assertEqual(bad.returncode, 64)
        # Without a compositor it reports the failure on stdout and exits 1.
        no_display = subprocess.run(
            [path, "--name", "Tandem", "--width", "8", "--height", "8"],
            capture_output=True, text=True, timeout=10,
            env=dict(os.environ, WAYLAND_DISPLAY="tandem-test-no-such-display",
                     XDG_RUNTIME_DIR=self.data_home))
        self.assertEqual(no_display.returncode, 1)
        self.assertTrue(no_display.stdout.startswith(
            "error cannot connect to the Wayland display"), no_display.stdout)


class DesktopEntryTest(ScratchDataHomeCase):
    def read_entry(self):
        with open(kwin_helper.desktop_entry_path()) as entry:
            return entry.read()

    def test_entry_names_the_binary_and_the_interfaces(self):
        path = "/home/user/.local/share/tandem/kwin-helper/tandem-kwin-helper"
        self.assertTrue(kwin_helper.ensure_desktop_entry(path))
        text = self.read_entry()
        self.assertIn(f"\nExec={path}\n", text)
        self.assertIn("\nNoDisplay=true\n", text)
        self.assertIn("\nX-KDE-Wayland-Interfaces=zkde_screencast_unstable_v1,"
                      "org_kde_kwin_fake_input\n", text)
        self.assertTrue(text.startswith("[Desktop Entry]\n"))
        self.assertNotIn("Hidden=", text)

    def test_unchanged_entry_is_not_rewritten(self):
        kwin_helper.ensure_desktop_entry("/opt/helper")
        inode = os.stat(kwin_helper.desktop_entry_path()).st_ino
        self.assertFalse(kwin_helper.ensure_desktop_entry("/opt/helper"))
        self.assertEqual(os.stat(kwin_helper.desktop_entry_path()).st_ino,
                         inode)

    def test_a_change_is_written_as_a_new_file(self):
        # KWin only notices a changed directory, not an in-place rewrite.
        kwin_helper.ensure_desktop_entry("/opt/helper")
        inode = os.stat(kwin_helper.desktop_entry_path()).st_ino
        self.assertTrue(kwin_helper.ensure_desktop_entry("/opt/other"))
        self.assertNotEqual(os.stat(kwin_helper.desktop_entry_path()).st_ino,
                            inode)
        self.assertIn("Exec=/opt/other\n", self.read_entry())
        leftovers = [name for name in os.listdir(os.path.dirname(
            kwin_helper.desktop_entry_path())) if name.startswith(".tandem-")]
        self.assertEqual(leftovers, [])

    def test_paths_with_spaces_are_quoted(self):
        kwin_helper.ensure_desktop_entry("/home/a user/helper")
        self.assertIn('Exec="/home/a user/helper"\n', self.read_entry())

    def test_paths_kwin_could_misread_are_refused(self):
        for path in ('/home/"x"/helper', "/home/$USER/helper",
                     "/home/100%/helper"):
            with self.subTest(path=path):
                with self.assertRaises(RuntimeError):
                    kwin_helper.ensure_desktop_entry(path)

    def test_remove_installed(self):
        self.fake_install("0" * 64)
        kwin_helper.ensure_desktop_entry(kwin_helper.helper_path())
        removed = kwin_helper.remove_installed()
        self.assertEqual(removed, [kwin_helper.desktop_entry_path(),
                                   kwin_helper.install_dir()])
        self.assertFalse(os.path.exists(kwin_helper.desktop_entry_path()))
        self.assertFalse(os.path.exists(kwin_helper.install_dir()))
        self.assertEqual(kwin_helper.remove_installed(), [])

    def test_remove_installed_also_clears_an_entry_in_our_data_home(self):
        ours = os.path.join(self.data_home, "applications",
                            "tandem-kwin-helper.desktop")
        kwin_helper.ensure_desktop_entry("/opt/helper")
        kwin_home = os.path.join(self.data_home, "kwin")
        launch = kwin_helper.system.ProcessLaunch(
            ["kwin_wayland_wrapper"], {"XDG_DATA_HOME": kwin_home}, "/")
        with mock.patch.object(kwin_helper.system, "socket_owner_launch",
                               return_value=launch):
            kwin_helper.ensure_desktop_entry("/opt/helper")
            removed = kwin_helper.remove_installed()
        self.assertEqual(removed, [os.path.join(
            kwin_home, "applications", "tandem-kwin-helper.desktop"), ours])
        self.assertFalse(os.path.exists(ours))


class KdeDependencyRowsTest(unittest.TestCase):
    def labels(self):
        return [row[0] for row in core.DEPS["kde"]]

    def test_rows(self):
        self.assertEqual(self.labels()[:5], [
            "kscreen-doctor", "C compiler", "pkg-config", "wayland-scanner",
            "wayland-client headers"])
        self.assertIn("gst-launch-1.0", self.labels())
        self.assertIn("H.264 encoder", self.labels())
        self.assertNotIn("python gi", self.labels())   # no D-Bus on KDE
        for row in core.DEPS["kde"]:
            with self.subTest(row=row[0]):
                self.assertEqual(set(row[2]), {"arch", "debian", "fedora"})
                self.assertTrue(row[3])

    def test_build_rows_pass_when_the_helper_is_already_built(self):
        build_rows = [row for row in core.DEPS["kde"]
                      if row[0] in ("C compiler", "pkg-config",
                                    "wayland-scanner",
                                    "wayland-client headers")]
        with mock.patch.object(kwin_helper, "is_current", return_value=True), \
                mock.patch.object(kwin_helper.shutil, "which",
                                  return_value=None):
            self.assertTrue(all(row[1]() for row in build_rows))
        with mock.patch.object(kwin_helper, "is_current",
                               return_value=False), \
                mock.patch.object(kwin_helper.shutil, "which",
                                  return_value=None):
            self.assertFalse(any(row[1]() for row in build_rows))

    def test_debian_package_follows_the_installed_plasma(self):
        def fake_glob(plasma):
            return lambda pattern: (["/usr/lib/x/lib.so.8"]
                                    if f"KF{plasma}Screen" in pattern else [])
        with mock.patch.object(core.glob, "glob", side_effect=fake_glob(5)):
            self.assertEqual(core._kscreen_doctor_debian_package(),
                             "libkf5screen-bin")
        with mock.patch.object(core.glob, "glob", side_effect=fake_glob(6)):
            self.assertEqual(core._kscreen_doctor_debian_package(),
                             "libkscreen-bin")
        with mock.patch.object(core.glob, "glob", return_value=[]):
            self.assertEqual(core._kscreen_doctor_debian_package(),
                             "libkscreen-bin")

    def test_missing_packages_per_distro(self):
        with mock.patch.object(core, "distro", return_value="fedora"), \
                mock.patch.object(kwin_helper, "is_current",
                                  return_value=False), \
                mock.patch.object(kwin_helper.shutil, "which",
                                  return_value=None), \
                mock.patch.object(core.shutil, "which", return_value=None):
            packages = core.missing_packages("kde", required_only=True)
        for package in ("libkscreen", "gcc", "pkgconf-pkg-config",
                        "wayland-devel", "gstreamer1"):
            with self.subTest(package=package):
                self.assertIn(package, packages)
        self.assertEqual(packages.count("wayland-devel"), 1)


if __name__ == "__main__":
    unittest.main()
