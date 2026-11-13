"""
kwin_helper.py - builds tandem-kwin-helper and lets KWin trust it.

The KDE backend (kde.py) creates its virtual monitor and injects touches
through a small C program, tandem-kwin-helper (source in linux/kwin-helper/),
because KWin only offers those features as Wayland protocols. It is compiled
on the user's machine, on first use and again whenever its sources change:

    wayland-scanner client-header / private-code   (vendored protocol XML)
    cc -std=c11 -O2 -Wall -Wextra tandem-kwin-helper.c <protocol code>
        $(pkg-config --cflags --libs wayland-client)
        -> ${XDG_DATA_HOME:-~/.local/share}/tandem/kwin-helper/
           tandem-kwin-helper

The binary lives outside tandem/linux (which install.sh replaces on every
update), at one stable path whether Tandem runs from a checkout or from an
install, because KWin identifies it by that path:

KWin 6.0 to 6.7 (and 5.27) only offer zkde_screencast_unstable_v1 and
org_kde_kwin_fake_input to an executable that a desktop file names in Exec=
and whose X-KDE-Wayland-Interfaces lists them. KWin compares the canonical
path of the client's /proc/<pid>/exe with the first word of Exec, so the
entry written here (<data home>/applications/tandem-kwin-helper.desktop)
names the binary by its absolute path. KWin finds entries through its own
XDG_DATA_HOME, which can differ from Tandem's (a shell rc file that sets it
isn't read by the login manager), so the data home is taken from KWin's
process (session_data_home()). KWin 6.8 dropped the check for unsandboxed
programs; the entry is harmless there.

KWin only re-reads applications when the directory changes, so the entry is
always written to a temporary file and renamed into place (an in-place
rewrite of an existing file goes unnoticed).

Everything here is used from the daemon (kde.py) and, without a daemon, from
core.DEPS (the TUI's dependency screen and install.sh): the queries are cheap
and never raise.
"""

import fcntl
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

from . import system
from .system import write_atomically

HELPER_NAME = "tandem-kwin-helper"
SOURCE_DIR = os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "kwin-helper")
SOURCE_FILE = HELPER_NAME + ".c"
# Wayland protocols the helper is compiled against (vendored in
# kwin-helper/protocols/, see the Makefile for why).
PROTOCOLS = ("zkde-screencast-unstable-v1", "fake-input",
             "xdg-output-unstable-v1")
# The compiler flags; the Makefile uses the same ones. No -Werror here: a
# warning a newer compiler invents must not stop users from building.
CFLAGS = ("-std=c11", "-O2", "-Wall", "-Wextra")
BUILD_TIMEOUT = 120             # seconds, per compiler / scanner run

# Interfaces the desktop entry asks KWin (6.7 and older) to grant.
WAYLAND_INTERFACES = ("zkde_screencast_unstable_v1", "org_kde_kwin_fake_input")
DESKTOP_FILE_NAME = HELPER_NAME + ".desktop"

# Characters that need no quoting in a desktop entry's Exec key.
_PLAIN_EXEC_CHARACTERS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789/._-+,:@")
# Characters a quoted Exec argument can't hold without escaping rules that
# KWin's own parser (QProcess::splitCommand) does not share.
_UNQUOTABLE_EXEC_CHARACTERS = frozenset('"\\`$%\n\r\t')


def log(*a):
    # Never raise: once the terminal or the TUI reading our stderr is gone,
    # a print would throw, and a failed log line must not fail a build.
    try:
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #

def data_home(environment=None):
    """$XDG_DATA_HOME of `environment` (default: ours), or ~/.local/share
    when it is unset or not absolute (the XDG spec says to ignore a relative
    one)."""
    environment = os.environ if environment is None else environment
    configured = environment.get("XDG_DATA_HOME", "")
    if os.path.isabs(configured):
        return configured
    home = environment.get("HOME") or os.path.expanduser("~")
    return os.path.join(home, ".local", "share")


def wayland_socket_path():
    """The Wayland display socket this process would connect to, or None."""
    display = os.environ.get("WAYLAND_DISPLAY", "")
    if not display:
        return None
    if os.path.isabs(display):
        return display
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "")
    return os.path.join(runtime_dir, display) if runtime_dir else None


def session_data_home():
    """The data home KWin itself uses, which is where it looks for desktop
    entries; data_home() when the compositor can't be asked.

    The two differ when a shell rc file sets XDG_DATA_HOME: the login
    manager never reads it, so Plasma (and KWin) keep ~/.local/share while
    a terminal, and Tandem started from it, see the other directory. KWin's
    environment is read from the process serving the Wayland socket
    (kwin_wayland_wrapper, whose environment KWin inherits).
    """
    launch = system.socket_owner_launch(wayland_socket_path())
    if launch is None:
        return data_home()
    return data_home(launch.environment)


def install_dir():
    return os.path.join(data_home(), "tandem", "kwin-helper")


def helper_path():
    """Where the built helper is (or will be) installed."""
    return os.path.join(install_dir(), HELPER_NAME)


def _hash_path():
    # The source hash of the installed binary, written after a good build.
    return os.path.join(install_dir(), "source.sha256")


def desktop_entry_path():
    """Where KWin looks for the helper's permission entry: in the
    applications directory of KWin's own data home (session_data_home())."""
    return os.path.join(session_data_home(), "applications",
                        DESKTOP_FILE_NAME)


def _desktop_entry_paths():
    """Every place a Tandem version may have written the entry: KWin's
    data home, and ours if it differs (older versions used ours)."""
    paths = [desktop_entry_path(),
             os.path.join(data_home(), "applications", DESKTOP_FILE_NAME)]
    return list(dict.fromkeys(paths))


def _source_files():
    """Every input of the build, in a fixed order."""
    return ([os.path.join(SOURCE_DIR, SOURCE_FILE)]
            + [os.path.join(SOURCE_DIR, "protocols", f"{protocol}.xml")
               for protocol in PROTOCOLS])


# --------------------------------------------------------------------------- #
# Build state (cheap queries, never raise)
# --------------------------------------------------------------------------- #

def source_hash():
    """SHA-256 over the sources and the compiler flags, or None when a
    source file can't be read (a broken checkout)."""
    digest = hashlib.sha256()
    digest.update(" ".join(CFLAGS).encode())
    try:
        for path in _source_files():
            digest.update(b"\0" + os.path.basename(path).encode() + b"\0")
            with open(path, "rb") as source:
                digest.update(source.read())
    except OSError:
        return None
    return digest.hexdigest()


def is_current():
    """True if the installed helper exists and was built from these sources
    (so nothing needs to be compiled)."""
    try:
        with open(_hash_path(), encoding="ascii") as stored:
            built_from = stored.read().strip()
    except (OSError, ValueError):
        return False
    current = source_hash()
    return (current is not None and built_from == current
            and os.access(helper_path(), os.X_OK))


def _compiler():
    return next((shutil.which(name) for name in ("cc", "gcc", "clang")
                 if shutil.which(name)), None)


def _wayland_client_available():
    """True if pkg-config knows libwayland-client (its headers are
    installed); False without pkg-config."""
    pkg_config = shutil.which("pkg-config")
    if not pkg_config:
        return False
    try:
        result = subprocess.run([pkg_config, "--exists", "wayland-client"],
                                capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


# What the build needs, as core.DEPS names it: tool -> (check, description).
BUILD_TOOLS = {
    "cc": (lambda: _compiler() is not None, "C compiler (cc)"),
    "pkg-config": (lambda: shutil.which("pkg-config") is not None,
                   "pkg-config"),
    "wayland-scanner": (lambda: shutil.which("wayland-scanner") is not None,
                        "wayland-scanner"),
    "wayland-client": (_wayland_client_available,
                       "wayland-client development files"),
}


def has_build_tool(tool):
    """True if build tool `tool` (a BUILD_TOOLS key) is installed."""
    check, _description = BUILD_TOOLS[tool]
    return check()


def build_problem():
    """What is missing to build the helper, as a list of descriptions
    (empty if nothing). Package names per distro are in core.DEPS."""
    missing = [description for check, description in BUILD_TOOLS.values()
               if not check()]
    if source_hash() is None:
        missing.append(f"helper sources ({SOURCE_DIR})")
    return missing


# --------------------------------------------------------------------------- #
# Building
# --------------------------------------------------------------------------- #

def _run_build_step(argv, step):
    """Run one build command; raise RuntimeError with its last output lines
    on failure."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True,
                                timeout=BUILD_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"could not build {HELPER_NAME}: {step} did not "
                           f"run ({error})") from error
    if result.returncode != 0:
        output = (result.stderr or result.stdout).strip().splitlines()
        tail = " | ".join(output[-6:]) or f"exit status {result.returncode}"
        raise RuntimeError(f"could not build {HELPER_NAME}: {step} failed "
                           f"({tail})")
    return result.stdout


def _compile_into(work_dir):
    """Generate the protocol code and compile the helper inside work_dir;
    returns the path of the new binary."""
    scanner = shutil.which("wayland-scanner")
    compiler = _compiler()
    if not scanner or not compiler or not shutil.which("pkg-config"):
        raise RuntimeError(f"could not build {HELPER_NAME}: missing "
                           + ", ".join(build_problem()))
    glue_sources = []
    for protocol in PROTOCOLS:
        xml = os.path.join(SOURCE_DIR, "protocols", f"{protocol}.xml")
        header = os.path.join(work_dir, f"{protocol}-client-protocol.h")
        glue = os.path.join(work_dir, f"{protocol}-protocol.c")
        _run_build_step([scanner, "client-header", xml, header],
                        f"wayland-scanner ({protocol})")
        _run_build_step([scanner, "private-code", xml, glue],
                        f"wayland-scanner ({protocol})")
        glue_sources.append(glue)
    wayland_flags = _run_build_step(
        ["pkg-config", "--cflags", "--libs", "wayland-client"],
        "pkg-config wayland-client").split()
    binary = os.path.join(work_dir, HELPER_NAME)
    _run_build_step([compiler, *CFLAGS, f"-I{work_dir}", "-o", binary,
                     os.path.join(SOURCE_DIR, SOURCE_FILE), *glue_sources,
                     *wayland_flags, "-lm"], "the C compiler")
    return binary


def ensure_built():
    """The path of an up-to-date helper, building it first when it is
    missing or its sources (C, protocol XML, flags) changed.

    Builds in a temporary directory and renames the binary into place, so
    a running helper is never overwritten and a failed build leaves the
    previous binary alone. A lock file keeps two daemons (or the TUI and a
    daemon) from building at the same time. Raises RuntimeError with the
    compiler's last lines when the build fails.
    """
    if is_current():
        return helper_path()
    expected_hash = source_hash()
    if expected_hash is None:
        raise RuntimeError(f"the {HELPER_NAME} sources are missing from "
                           f"{SOURCE_DIR}; reinstall Tandem")
    try:
        os.makedirs(install_dir(), exist_ok=True)
        lock = open(os.path.join(install_dir(), ".build.lock"), "w")
    except OSError as error:
        raise RuntimeError(f"could not create {install_dir()} ({error})") \
            from error
    with lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if is_current():                 # built while we waited
            return helper_path()
        log(f"building {HELPER_NAME} (KWin virtual monitor helper)")
        with tempfile.TemporaryDirectory(prefix="tandem-kwin-build-") as work:
            binary = _compile_into(work)
            try:
                os.chmod(binary, 0o755)
                # Same filesystem as the target, then an atomic rename.
                staged = helper_path() + ".new"
                shutil.copy2(binary, staged)
                os.replace(staged, helper_path())
                write_atomically(_hash_path(), expected_hash + "\n")
            except OSError as error:
                raise RuntimeError(f"could not install {HELPER_NAME} into "
                                   f"{install_dir()} ({error})") from error
    log(f"built {helper_path()}")
    return helper_path()


# --------------------------------------------------------------------------- #
# KWin's permission entry
# --------------------------------------------------------------------------- #

def _exec_value(path):
    """`path` as the Exec value KWin matches against the running binary."""
    if set(path) <= _PLAIN_EXEC_CHARACTERS:
        return path
    if set(path) & _UNQUOTABLE_EXEC_CHARACTERS:
        raise RuntimeError(f"the path {path!r} can't be written into a "
                           "desktop entry; set XDG_DATA_HOME to a plain path")
    return f'"{path}"'


def desktop_entry_text(path):
    """The desktop entry that names `path` as the program KWin may give the
    screencast and fake-input protocols to."""
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            "Name=Tandem KWin helper\n"
            "Comment=Creates Tandem's phone monitor and its touch input "
            "on KDE Plasma\n"
            f"Exec={_exec_value(path)}\n"
            # Not an app to launch: keep it out of menus and search. KWin
            # still reads it (Hidden=true or NotShowIn=KDE would hide it
            # from KWin too).
            "NoDisplay=true\n"
            "Terminal=false\n"
            "X-KDE-Wayland-Interfaces=" + ",".join(WAYLAND_INTERFACES) + "\n")


def ensure_desktop_entry(path):
    """Write the permission entry for the helper at `path`, if it isn't
    already there with exactly this content. Returns True if it was
    (re)written. Raises RuntimeError when it can't be written."""
    text = desktop_entry_text(path)
    entry = desktop_entry_path()
    try:
        with open(entry, encoding="utf-8") as existing:
            if existing.read() == text:
                return False
    except (OSError, ValueError):
        pass                              # missing or unreadable: write it
    try:
        # A new file, renamed into place: KWin notices a changed
        # directory, not an in-place rewrite.
        write_atomically(entry, text, make_dirs=True)
    except OSError as error:
        raise RuntimeError(f"could not write {entry} ({error})") from error
    log(f"wrote {entry} (lets KWin 6.7 and older grant the helper "
        "its protocols)")
    return True


def remove_installed():
    """Remove the desktop entry and the built helper (install.sh
    --uninstall). Returns the paths removed; missing ones are skipped.
    Raises OSError when one exists but can't be removed."""
    removed = []
    for entry in _desktop_entry_paths():
        try:
            os.unlink(entry)
            removed.append(entry)
        except FileNotFoundError:
            pass
    if os.path.isdir(install_dir()):
        shutil.rmtree(install_dir(), ignore_errors=True)
        removed.append(install_dir())
    return removed
