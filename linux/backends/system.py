"""
system.py - small local-system helpers shared by the backends.

* write_atomically(): replace a file in one step. Compositors watch the files
  Tandem writes (KWin's KSycoca notices a renamed desktop entry but not an
  in-place rewrite), so they must only ever see the old file or the new one.
* socket_peer_pid() and read_process_launch(): which process serves a UNIX
  socket, and how it was started. A compositor's settings can live in its own
  environment and command line, which the programs it starts don't inherit
  unchanged (a Plasma session's XDG_DATA_HOME can differ from a terminal's
  after a shell rc file changed it), so the backends ask the compositor's
  process instead of trusting their own environment.

Queries return None on any failure and never raise.
"""

import os
import socket
import struct
import tempfile
from typing import NamedTuple

PEER_TIMEOUT = 2.0              # seconds to connect to a local socket
PROC_ROOT = "/proc"


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

def write_atomically(path, text, mode=0o644, make_dirs=False):
    """Replace `path` with `text`: written to a temporary file next to it,
    then renamed over it, so readers see the old or the new file, never half
    of one. `make_dirs` creates the directory first. Raises OSError."""
    directory = os.path.dirname(path)
    if make_dirs:
        os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".tandem-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


# --------------------------------------------------------------------------- #
# The process behind a socket
# --------------------------------------------------------------------------- #

class ProcessLaunch(NamedTuple):
    """How a running process was started, as /proc reports it."""
    argv: list              # its command line
    environment: dict       # the environment it started with
    cwd: str                # its working directory ("" if unreadable)


def socket_peer_pid(path):
    """PID of the process listening on UNIX socket `path`, or None.

    Asked of the kernel (SO_PEERCRED on a connection to it), so it is the
    process that created the socket, whatever launched it. A process of
    another user counts as none: its files aren't ours to read or edit.
    The connection is closed without sending anything, which a Wayland
    compositor treats as an ordinary disconnect.
    """
    if not path:
        return None
    credentials_size = struct.calcsize("3i")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(PEER_TIMEOUT)
            sock.connect(path)
            credentials = sock.getsockopt(socket.SOL_SOCKET,
                                          socket.SO_PEERCRED,
                                          credentials_size)
    except (OSError, TypeError, ValueError):
        return None
    pid, uid, _gid = struct.unpack("3i", credentials)
    if pid <= 0 or uid != os.getuid():
        return None
    return pid


def read_process_launch(pid, proc_root=PROC_ROOT):
    """ProcessLaunch of process `pid`, or None if /proc can't tell.

    /proc/<pid>/environ is the environment the process *started* with:
    unsetenv() or setenv() inside the process doesn't change it, which is
    what makes it useful here.
    """
    base = os.path.join(proc_root, str(pid))
    try:
        with open(os.path.join(base, "cmdline"), "rb") as cmdline:
            argv = [argument.decode("utf-8", "surrogateescape")
                    for argument in cmdline.read().split(b"\0") if argument]
        with open(os.path.join(base, "environ"), "rb") as environ:
            entries = environ.read().split(b"\0")
    except OSError:
        return None
    if not argv:
        return None                     # a zombie, or a kernel thread
    environment = {}
    for entry in entries:
        name, separator, value = entry.decode(
            "utf-8", "surrogateescape").partition("=")
        if separator:
            environment[name] = value
    try:
        cwd = os.readlink(os.path.join(base, "cwd"))
    except OSError:
        cwd = ""
    return ProcessLaunch(argv, environment, cwd)


def socket_owner_launch(path):
    """ProcessLaunch of the process listening on `path`, or None."""
    pid = socket_peer_pid(path)
    return read_process_launch(pid) if pid else None
