"""Tests for the parts outside the backends that depend on which desktop is
running: the package rows for KDE Plasma and niri (names checked against the
distributions' repositories), the TUI's touch-setup line and Dependencies
notes, the desktop list in user-facing messages, and install.sh's syntax."""

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


class NiriPackageRowsTest(unittest.TestCase):
    def test_rows(self):
        packages = packages_by_label("niri")
        self.assertEqual(list(packages)[:3], ["niri", "gst-launch-1.0",
                                              "python gi"])
        # Debian and Ubuntu have no niri package (sources.debian.org and
        # Launchpad, 2026-11-10); Arch and Fedora do.
        self.assertEqual(packages["niri"], {"arch": "niri",
                                            "debian": core.NOT_PACKAGED,
                                            "fedora": "niri"})
        self.assertEqual(packages["python gi"]["fedora"], "python3-gobject")

    def debian_rows(self, present):
        """check_dependencies("niri") on Debian with only `present`
        installed (by row label)."""
        rows = [(label, (lambda label=label: label in present), packages,
                 required, purpose)
                for label, _check, packages, required, purpose
                in core.DEPS["niri"]]
        return [mock.patch.object(core, "distro", return_value="debian"),
                mock.patch.dict(core.DEPS, {"niri": rows,
                                            "common": []})]

    def test_debian_installs_the_gstreamer_rows_but_not_niri(self):
        patches = self.debian_rows(present={"python gi"})
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        packages = core.missing_packages("niri", required_only=True)
        self.assertNotIn("niri", packages)
        self.assertNotIn(None, packages)
        self.assertIn("gstreamer1.0-tools", packages)
        self.assertIn("gstreamer1.0-pipewire", packages)
        self.assertEqual(core.unpackaged_missing("niri"), ["niri"])

    def test_debian_with_niri_built_from_source(self):
        labels = [row[0] for row in core.DEPS["niri"]]
        patches = self.debian_rows(present=set(labels))
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.assertEqual(core.missing_packages("niri"), [])
        self.assertEqual(core.unpackaged_missing("niri"), [])

    def test_arch_and_fedora_install_niri(self):
        for distro in ("arch", "fedora"):
            with self.subTest(distro=distro), \
                    mock.patch.object(core, "distro", return_value=distro):
                rows = {row[0]: row[1]
                        for row in core.check_dependencies("niri")}
                self.assertEqual(rows["niri"], "niri")


class DesktopListTest(unittest.TestCase):
    def test_unsupported_message_names_every_desktop(self):
        text = backends.supported_desktops()
        for desktop in ("Hyprland", "GNOME", "KDE Plasma", "niri"):
            self.assertIn(desktop, text)

    def test_tui_backend_choices(self):
        self.assertEqual(list(backends.NAMES),
                         ["hyprland", "gnome", "kde", "niri"])


class TouchLineTest(unittest.TestCase):
    def setUp(self):
        self.cfg = core.Config(backend="niri")
        self.stream = mock.Mock(touch_status="")

    def test_runnable_fix_says_press_t(self):
        setup = {"problem": "uinput is not writable",
                 "fix": ["sudo", "sh", "-c", "true"]}
        text, color = tui.touch_line(self.cfg, self.stream, setup)
        self.assertIn("press t", text)
        self.assertEqual(color, 4)

    def test_no_fix_does_not_offer_t(self):
        setup = {"problem": "niri's config file doesn't exist yet",
                 "fix": None}
        text, _color = tui.touch_line(self.cfg, self.stream, setup)
        self.assertIn("niri's config file", text)
        self.assertNotIn("press t", text)

    def test_a_long_problem_moves_below_the_row(self):
        setup = {"problem": "niri's config needs the line include "
                            'optional=true "tandem.kdl" so touches land on '
                            "the phone's monitor; niri's config file "
                            "/home/u/.config/niri/config.kdl doesn't exist "
                            "yet (start niri once to create it)",
                 "fix": None}
        row, color, details = tui.touch_row(self.cfg, self.stream, setup, 60)
        self.assertLessEqual(len(row), 60)
        self.assertEqual(color, 4)
        self.assertTrue(all(len(line) <= 60 for line in details))
        self.assertIn("(start niri once to create it)", " ".join(details))
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
        notes = tui.backend_notes("kde", "arch")
        self.assertEqual(len(notes), 1)
        self.assertIn("KWin helper", notes[0])

    def test_niri_notes_depend_on_the_distro(self):
        self.assertEqual(len(tui.backend_notes("niri", "arch")), 1)
        self.assertIn("#3800", tui.backend_notes("niri", "arch")[0])
        debian = tui.backend_notes("niri", "debian")
        self.assertEqual(len(debian), 2)
        self.assertIn("don't package niri", debian[1])

    def test_no_notes_for_hyprland_and_gnome(self):
        for backend in ("hyprland", "gnome", None):
            with self.subTest(backend=backend):
                self.assertEqual(tui.backend_notes(backend, "fedora"), [])


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

    def test_uninstall_removes_the_kwin_helper_entry(self):
        with open(INSTALL_SCRIPT, encoding="utf-8") as script:
            text = script.read()
        # Same name as backends/kwin_helper.py writes.
        self.assertIn(f'"$APPS/{kwin_helper.DESKTOP_FILE_NAME}"', text)


if __name__ == "__main__":
    unittest.main()
