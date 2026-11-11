"""Tests for the parts outside the backends that depend on which desktop is
running: the package rows for KDE Plasma (names checked against the
distributions' repositories), the TUI's touch-setup line and Dependencies
notes, the desktop list and beta labels in user-facing messages, and
install.sh's syntax and release download."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import backends
import core
import tui
from backends import kwin_helper

INSTALL_SCRIPT = os.path.join(support.REPO_DIR, "install.sh")


def packages_by_label(backend):
    return {label: packages for label, _check, packages, _required, _purpose
            in core.DEPS[backend]}


class KdePackageRowsTest(unittest.TestCase):
    """Package names verified with pacman -F (Arch), dnf provides (Fedora 43)
    and apt-file (Debian 13, Ubuntu 24.04) on 2026-11-09."""

    def test_rows_in_order(self):
        self.assertEqual(list(packages_by_label("kde")),
                         ["kscreen-doctor", "C compiler", "pkg-config",
                          "wayland-scanner", "wayland-client headers",
                          "gst-launch-1.0", "pipewiresrc", "videoconvert",
                          "h264parse", "H.264 encoder"])

    def test_every_kde_row_is_required(self):
        self.assertTrue(all(row[3] for row in core.DEPS["kde"]))

    def test_kscreen_doctor_packages(self):
        packages = packages_by_label("kde")["kscreen-doctor"]
        self.assertEqual(packages["arch"], "libkscreen")
        self.assertEqual(packages["fedora"], "libkscreen")
        self.assertIn(packages["debian"], ("libkscreen-bin", "libkf5screen-bin"))

    def test_debian_kscreen_doctor_follows_the_installed_libkscreen(self):
        def installed(*libraries):
            return lambda pattern: [pattern] if any(
                library in pattern for library in libraries) else []

        cases = [((), "libkscreen-bin"),                  # Plasma not found
                 (("libKF6Screen",), "libkscreen-bin"),   # Debian 13, Kubuntu 25.04+
                 (("libKF5Screen",), "libkf5screen-bin"),  # Kubuntu 24.04
                 (("libKF5Screen", "libKF6Screen"), "libkscreen-bin")]
        for libraries, expected in cases:
            with self.subTest(libraries=libraries):
                with mock.patch.object(core.glob, "glob",
                                       side_effect=installed(*libraries)):
                    self.assertEqual(core._kscreen_doctor_debian_package(),
                                     expected)

    def test_helper_build_packages(self):
        packages = packages_by_label("kde")
        self.assertEqual({label: packages[label]["arch"] for label in
                          ("C compiler", "pkg-config", "wayland-scanner",
                           "wayland-client headers")},
                         {"C compiler": "gcc", "pkg-config": "pkgconf",
                          "wayland-scanner": "wayland",
                          "wayland-client headers": "wayland"})
        self.assertEqual(packages["wayland-scanner"]["fedora"], "wayland-devel")
        self.assertEqual(packages["wayland-client headers"]["fedora"],
                         "wayland-devel")
        self.assertEqual(packages["pkg-config"]["fedora"], "pkgconf-pkg-config")
        self.assertEqual(packages["wayland-scanner"]["debian"], "libwayland-bin")
        self.assertEqual(packages["wayland-client headers"]["debian"],
                         "libwayland-dev")

    def test_no_python_gi_for_kde(self):
        # KDE talks to KWin through the helper, never over D-Bus.
        self.assertNotIn("python gi", packages_by_label("kde"))


class DesktopListTest(unittest.TestCase):
    def test_unsupported_message_names_every_desktop(self):
        text = backends.supported_desktops()
        for desktop in ("Hyprland", "GNOME", "KDE Plasma"):
            self.assertIn(desktop, text)

    def test_tui_backend_choices(self):
        self.assertEqual(list(backends.NAMES), ["hyprland", "gnome", "kde"])


class BetaLabelTest(unittest.TestCase):
    """KDE Plasma is in beta: every place a user meets the backend says so,
    and the stable backends say nothing of the kind."""

    def test_kde_is_labelled_beta(self):
        self.assertIn("beta", backends.describe("kde"))
        self.assertIn("KDE Plasma on Wayland (beta)",
                      backends.supported_desktops())
        notice = backends.beta_notice("kde")
        self.assertIn("KDE Plasma support is in beta", notice)
        self.assertIn(backends.ISSUES_URL, notice)

    def test_stable_backends_are_not(self):
        for name in ("hyprland", "gnome", None, "unknown"):
            with self.subTest(name=name):
                self.assertIsNone(backends.beta_notice(name))
                self.assertNotIn("beta", backends.describe(name))


class TouchLineTest(unittest.TestCase):
    def setUp(self):
        self.cfg = core.Config(backend="hyprland")
        self.stream = mock.Mock(touch_status="")

    def test_runnable_fix_says_press_t(self):
        setup = {"problem": "uinput is not writable",
                 "fix": ["sudo", "sh", "-c", "true"]}
        text, color = tui.touch_line(self.cfg, self.stream, setup)
        self.assertIn("press t", text)
        self.assertEqual(color, 4)

    def test_no_fix_does_not_offer_t(self):
        setup = {"problem": "no permission to use /dev/uinput",
                 "fix": None}
        text, _color = tui.touch_line(self.cfg, self.stream, setup)
        self.assertIn("/dev/uinput", text)
        self.assertNotIn("press t", text)

    def test_a_long_problem_moves_below_the_row(self):
        setup = {"problem": "no permission to use /dev/uinput, so the "
                            "phone's touches can't reach the desktop; a "
                            "udev rule grants it to whoever is logged in "
                            "at the seat (log out and back in afterwards)",
                 "fix": None}
        row, color, details = tui.touch_row(self.cfg, self.stream, setup, 60)
        self.assertLessEqual(len(row), 60)
        self.assertEqual(color, 4)
        self.assertTrue(all(len(line) <= 60 for line in details))
        self.assertIn("(log out and back in afterwards)", " ".join(details))
        setup["fix"] = ["sh", "-c", "true"]
        row, _color, _details = tui.touch_row(self.cfg, self.stream, setup,
                                              60)
        self.assertTrue(row.endswith("press t"))

    def test_a_short_problem_stays_on_the_row(self):
        setup = {"problem": "no permission to use /dev/uinput", "fix": None}
        row, _color, details = tui.touch_row(self.cfg, self.stream, setup, 80)
        self.assertIn("no permission to use /dev/uinput", row)
        self.assertEqual(details, [])

    def test_ready_and_off(self):
        self.stream.touch_status = "ready (KWin fake input on Virtual-Tandem)"
        self.assertEqual(tui.touch_line(self.cfg, self.stream, None)[1], 3)
        self.cfg.touch = False
        self.assertIn("off", tui.touch_line(self.cfg, self.stream, None)[0])


class BackendNotesTest(unittest.TestCase):
    def test_kde_note_mentions_the_helper(self):
        notes = tui.backend_notes("kde")
        self.assertEqual(len(notes), 2)
        self.assertIn("KWin helper", notes[0])

    def test_kde_note_says_beta_and_where_to_report(self):
        beta = tui.backend_notes("kde")[1]
        self.assertIn("in beta", beta)
        self.assertIn(backends.ISSUES_URL, beta)

    def test_no_notes_for_hyprland_and_gnome(self):
        for backend in ("hyprland", "gnome", None):
            with self.subTest(backend=backend):
                self.assertEqual(tui.backend_notes(backend), [])


@unittest.skipUnless(shutil.which("bash"), "bash is not installed")
class InstallScriptTest(unittest.TestCase):
    def test_syntax(self):
        result = subprocess.run(["bash", "-n", INSTALL_SCRIPT],
                                capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def session_problem_snippet(self):
        """The Python that install.sh runs to print the session problem."""
        with open(INSTALL_SCRIPT, encoding="utf-8") as script:
            text = script.read()
        match = re.search(r"""\"\$PY\" -c '\n(import sys\ntry:\n    import """
                          r"""backends\n.*?)' \"\$backend\"""", text, re.S)
        self.assertIsNotNone(match, "session-problem one-liner not found")
        return match.group(1)

    def run_against(self, backends_source, snippet):
        """Run `snippet` with a `backends` package made of
        `backends_source`, as from an older release's tarball."""
        root = tempfile.mkdtemp(prefix="tandem-old-release-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        os.makedirs(os.path.join(root, "backends"))
        with open(os.path.join(root, "backends", "__init__.py"), "w",
                  encoding="utf-8") as package:
            package.write(backends_source)
        return subprocess.run([sys.executable, "-c", snippet, "gnome"],
                              cwd=root, capture_output=True, text=True,
                              timeout=30, env=dict(os.environ, PYTHONPATH=root))

    def test_session_problem_with_an_older_release_says_nothing(self):
        # 0.6.2's backends package has no module(): no traceback, no lines.
        result = self.run_against("NAMES = ('hyprland', 'gnome')\n",
                                  self.session_problem_snippet())
        self.assertEqual((result.returncode, result.stdout, result.stderr),
                         (0, "", ""))

    def test_session_problem_lines_are_printed(self):
        result = self.run_against(
            "import types\n"
            "def module(name):\n"
            "    return types.SimpleNamespace(session_problem=lambda: "
            "['on X11', 'pick Wayland'])\n",
            self.session_problem_snippet())
        self.assertEqual(result.stdout, "on X11\npick Wayland\n")

    def test_supported_desktops_falls_back_quietly(self):
        with open(INSTALL_SCRIPT, encoding="utf-8") as script:
            text = script.read()
        self.assertIn("print(backends.supported_desktops())' 2>/dev/null "
                      '|| echo "', text)

    def run_download_step(self, version):
        """Run a copy of install.sh away from any checkout, so it downloads,
        with a stand-in `curl` that records each URL and fails like a 404.
        Returns (exit status, stderr, the URLs asked for). The script stops
        at the download, before it changes anything."""
        root = tempfile.mkdtemp(prefix="tandem-install-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        script = os.path.join(root, "install.sh")
        shutil.copy(INSTALL_SCRIPT, script)
        tools = os.path.join(root, "bin")
        os.makedirs(tools)
        urls = os.path.join(root, "urls")
        with open(os.path.join(tools, "curl"), "w", encoding="utf-8") as curl:
            curl.write('#!/bin/sh\nfor a; do case "$a" in https://*) '
                       f'echo "$a" >> {urls};; esac; done\nexit 22\n')
        os.chmod(os.path.join(tools, "curl"), 0o755)
        environment = {"PATH": tools + os.pathsep + os.environ["PATH"],
                       "HOME": os.path.join(root, "home"), "TMPDIR": root}
        if version is not None:
            environment["TANDEM_VERSION"] = version
        result = subprocess.run(["bash", script], capture_output=True,
                                text=True, timeout=30, env=environment)
        asked = []
        if os.path.exists(urls):
            with open(urls, encoding="utf-8") as f:
                asked = f.read().split()
        self.assertFalse(os.path.exists(os.path.join(root, "home")))
        return result.returncode, result.stderr, asked

    def test_latest_release_then_main(self):
        status, _stderr, asked = self.run_download_step(None)
        self.assertNotEqual(status, 0)
        self.assertEqual(asked, [
            "https://github.com/saharshbaiju/tandem/releases/latest/download/"
            "tandem-linux.tar.gz",
            "https://github.com/saharshbaiju/tandem/archive/refs/heads/"
            "main.tar.gz"])

    def test_tandem_version_fetches_that_release_only(self):
        status, stderr, asked = self.run_download_step("v0.7.0-beta.1")
        self.assertEqual(status, 1)
        self.assertEqual(asked, [
            "https://github.com/saharshbaiju/tandem/releases/download/"
            "v0.7.0-beta.1/tandem-linux.tar.gz"])
        self.assertIn("Release v0.7.0-beta.1 has no tandem-linux.tar.gz",
                      stderr)

    def test_tandem_version_must_look_like_a_tag(self):
        for version in ("v1/../../x", "v1 2", "v1?x=1"):
            with self.subTest(version=version):
                status, stderr, asked = self.run_download_step(version)
                self.assertEqual((status, asked), (1, []))
                self.assertIn("must be a release tag", stderr)

    def test_uninstall_removes_the_kwin_helper_entry(self):
        with open(INSTALL_SCRIPT, encoding="utf-8") as script:
            text = script.read()
        # Same name as backends/kwin_helper.py writes.
        self.assertIn(f'"$APPS/{kwin_helper.DESKTOP_FILE_NAME}"', text)


if __name__ == "__main__":
    unittest.main()
