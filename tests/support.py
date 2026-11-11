"""
support.py - shared setup for the unit tests; every test module imports it
first.

* Puts <repo>/linux on sys.path, so the tests import the daemon's modules
  exactly as tandemd.py and tui.py do (`import core`, `from backends import
  gnome`).
* Points HOME (and the XDG directories) at a throwaway directory before any
  of those modules is imported: secure_link computes its config paths from
  HOME at import time and migrates an old config directory there, and no
  test may read or write the real user's pairing key or config.
* Removes NIRI_SOCKET and WAYLAND_DISPLAY (and moves XDG_RUNTIME_DIR), so
  no test talks to the real compositor.
"""

import atexit
import os
import shutil
import sys
import tempfile

REPO_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LINUX_DIR = os.path.join(REPO_DIR, "linux")
FIXTURES_DIR = os.path.join(REPO_DIR, "tests", "fixtures")

if LINUX_DIR not in sys.path:
    sys.path.insert(0, LINUX_DIR)

if "secure_link" in sys.modules:
    raise RuntimeError("import tests/support.py before any Tandem module")

SCRATCH_HOME = tempfile.mkdtemp(prefix="tandem-tests-home-")
atexit.register(shutil.rmtree, SCRATCH_HOME, ignore_errors=True)
os.environ["HOME"] = SCRATCH_HOME
os.environ["XDG_CONFIG_HOME"] = os.path.join(SCRATCH_HOME, ".config")
os.environ["XDG_DATA_HOME"] = os.path.join(SCRATCH_HOME, ".local", "share")
# Tests run inside a desktop session must not reach the real compositor (the
# niri and KDE backends ask its process where its config and desktop entries
# live): no niri socket, no Wayland display, and a runtime directory of our
# own. Tests that need one set these to fakes.
os.environ.pop("NIRI_SOCKET", None)
os.environ.pop("WAYLAND_DISPLAY", None)
os.environ["XDG_RUNTIME_DIR"] = os.path.join(SCRATCH_HOME, "runtime")
