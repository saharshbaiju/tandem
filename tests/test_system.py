"""Tests for backends/system.py, the local-system helpers the KDE backend
uses to find KWin's data directory: replacing a file atomically, the process
behind a UNIX socket (a real socket in a temporary directory), and reading a
process's start-up command line and environment from a fake /proc."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import os
import shutil
import socket
import stat
import tempfile
import unittest
from unittest import mock

from backends import system


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="tandem-system-")
        self.addCleanup(shutil.rmtree, self.root, ignore_errors=True)


class WriteAtomicallyTest(TempDirTestCase):
    def test_replaces_the_file_and_leaves_no_temporary(self):
        path = os.path.join(self.root, "entry.desktop")
        system.write_atomically(path, "old\n")
        system.write_atomically(path, "new\n", mode=0o600)
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "new\n")
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        self.assertEqual(os.listdir(self.root), ["entry.desktop"])

    def test_make_dirs(self):
        path = os.path.join(self.root, "a", "b", "file")
        with self.assertRaises(OSError):
            system.write_atomically(path, "x")
        system.write_atomically(path, "x", make_dirs=True)
        self.assertTrue(os.path.isfile(path))

    def test_a_failed_write_keeps_the_old_file(self):
        path = os.path.join(self.root, "file")
        system.write_atomically(path, "old")
        with mock.patch.object(system.os, "replace",
                               side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                system.write_atomically(path, "new")
        with open(path, encoding="utf-8") as f:
            self.assertEqual(f.read(), "old")
        self.assertEqual(os.listdir(self.root), ["file"])


class SocketPeerTest(TempDirTestCase):
    def test_the_listening_process_is_found(self):
        path = os.path.join(self.root, "wayland-0")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.addCleanup(listener.close)
        listener.bind(path)
        listener.listen(1)
        self.assertEqual(system.socket_peer_pid(path), os.getpid())

    def test_no_socket_is_none(self):
        self.assertIsNone(system.socket_peer_pid(
            os.path.join(self.root, "missing")))
        self.assertIsNone(system.socket_peer_pid(""))
        self.assertIsNone(system.socket_peer_pid(None))


class ReadProcessLaunchTest(TempDirTestCase):
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

    def test_command_line_environment_and_cwd(self):
        proc = self.fake_proc(4242, ["kwin_wayland", "--xwayland"],
                              {"XDG_DATA_HOME": "/home/u/data",
                               "EMPTY": "", "OPTIONS": "a=b"},
                              cwd=self.root)
        launch = system.read_process_launch(4242, proc_root=proc)
        self.assertEqual(launch.argv, ["kwin_wayland", "--xwayland"])
        self.assertEqual(launch.environment["XDG_DATA_HOME"], "/home/u/data")
        self.assertEqual(launch.environment["EMPTY"], "")
        self.assertEqual(launch.environment["OPTIONS"], "a=b")
        self.assertEqual(launch.cwd, self.root)

    def test_unreadable_cwd_is_empty(self):
        proc = self.fake_proc(5, ["kwin_wayland"], {})
        self.assertEqual(system.read_process_launch(5, proc_root=proc).cwd,
                         "")

    def test_unreadable_or_empty_entries_are_none(self):
        self.assertIsNone(system.read_process_launch(
            1, proc_root=os.path.join(self.root, "nothing")))
        proc = self.fake_proc(7, [], {})
        self.assertIsNone(system.read_process_launch(7, proc_root=proc))

    def test_socket_owner_launch_reads_the_owner(self):
        with mock.patch.object(system, "socket_peer_pid",
                               return_value=os.getpid()):
            launch = system.socket_owner_launch("/run/user/1000/wayland-0")
        self.assertIsNotNone(launch)
        self.assertTrue(launch.argv)
        with mock.patch.object(system, "socket_peer_pid", return_value=None):
            self.assertIsNone(system.socket_owner_launch("/nowhere"))


if __name__ == "__main__":
    unittest.main()
