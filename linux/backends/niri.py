"""
niri.py - niri backend: a virtual output on niri, captured through PipeWire.

EXPERIMENTAL: virtual outputs exist only in niri pull request #3800
("virtual outputs"), which no niri release has yet (26.04 is the latest).
Tandem detects support at runtime: an older niri answers the request with
"error parsing request", and setup() then says that this niri can't create
virtual monitors. The request names may still change before the PR merges.

Everything is driven over niri's IPC socket ($NIRI_SOCKET: one JSON request
per line, one JSON reply per line), with the standard library only:

    {"CreateVirtualOutput": {width, height, refresh_rate, name: "Tandem"}}
      -> {"Output": {"output": "Tandem", "action": CustomMode / Scale /
                     Position}}       mode, scale, and where the phone sits
      -> org.gnome.Mutter.ScreenCast (niri implements Mutter's D-Bus API):
         CreateSession -> RecordMonitor("Tandem") -> Start
         -> PipeWireStreamAdded(node)
      -> gst-launch-1.0 pipewiresrc path=<node> ! ... H.264 ... ! fdsink
         (gstreamer.py, the same pipeline GNOME and KDE use)
    {"RemoveVirtualOutput": {"name": "Tandem"}}        on teardown

The output does NOT die with our connection: a daemon that is killed leaves
"Tandem" behind. So setup() removes a leftover one first (recognised by
make "niri" and model "Tandem", which is how niri labels virtual outputs),
teardown() always runs (honors_keep_output = False), and the TUI's
remove_leftover_output() hook cleans up after a daemon that crashed.

Placement. niri lays out outputs with an explicit position first, then the
automatic ones, each to the right of everything placed so far. So:
  right            -> automatic position: niri puts it right of everything
                      and never moves the user's monitors for it;
  left / up / down -> an explicit position beside the main monitor. If the
                      user's monitors are positioned automatically, niri may
                      then move them; we read the layout back and, if any
                      other output moved, go back to automatic (the right
                      side) with a warning instead of rearranging the desk.
niri forgets settings made over IPC when its config is reloaded with output
changes, so an event-stream watcher re-applies mode, scale and position on
every ConfigLoaded event.

Golden rule (as for Hyprland): Tandem only ever changes its own output.

Touch. niri has no protocol to inject touches, so the phone's touches go
through the same uinput touchscreen as on Hyprland (uinput_touch.py). niri
maps touchscreens to an output only through its config, and the setting is
global (`input { touch { map-to-output } }` applies to every touchscreen).
Tandem therefore owns one small file, tandem.kdl next to config.kdl, which
holds that mapping while the phone streams and is emptied afterwards; the
user's config.kdl needs one line, added once by the touch setup (key t in
the TUI, or install.sh):

    include optional=true "tandem.kdl"

niri watches included files and reloads them live. An included `touch {}`
section replaces the user's own one rather than merging with it (niri treats
pointing-device sections as a whole), which is harmless here: on a computer
with a real touchscreen touch from the phone stays off, because the mapping
would send the real screen's touches to the phone's monitor too.
"""

import json
import os
import re
import shlex
import socket
import stat
import struct
import sys
import threading
import time
from typing import NamedTuple

import uinput_touch
from uinput_touch import uinput_setup_problem

from .gnome import CURSOR_EMBEDDED, SC_BUS, SC_IFACE, SC_PATH, Gio, GLib
from .gnome import _call as _dbus_call
from .gstreamer import (choose_encoder, missing_pipeline_tools,
                        start_encoder, stop_pipeline)
from .layout import place_beside, side_phrase
from .system import (read_process_launch, socket_peer_pid,
                     write_atomically)

OUTPUT_NAME = "Tandem"          # niri labels it make "niri", model "Tandem"
VIRTUAL_MAKE = "niri"

IPC_TIMEOUT = 5.0               # seconds for one request / reply
APPEAR_TIMEOUT = 2.0            # for a new output to show up in Outputs
SETTLE_TIMEOUT = 1.5            # for an output change to show up in Outputs
POLL_INTERVAL = 0.05
AUTOMATIC = "automatic"         # niri places the output itself (right side)
STREAM_TIMEOUT = 10.0           # for niri to announce the PipeWire stream

# What niri answers for a request it doesn't know: a niri without virtual
# outputs (every release up to 26.04) for CreateVirtualOutput.
UNKNOWN_REQUEST = "error parsing request"

# Scales niri can represent are multiples of 1/120 (fractional-scale
# protocol); anything within half a step is the scale we asked for.
SCALE_TOLERANCE = 1 / 240

# Touch mapping: the Tandem-owned config file and the include line for it.
TOUCH_FILE_NAME = "tandem.kdl"
INCLUDE_LINE = f'include optional=true "{TOUCH_FILE_NAME}"'
SYSTEM_CONFIG = "/etc/niri/config.kdl"
TOUCH_FILE_HEADER = (
    "// Managed by Tandem (phone as a second monitor); do not edit.\n"
    "// While Tandem streams, this maps its virtual touchscreen to the\n"
    "// phone's output; it is empty otherwise. config.kdl includes it with:\n"
    f"//     {INCLUDE_LINE}\n")
TOUCH_FILE_CLEARED = TOUCH_FILE_HEADER
TOUCH_FILE_MAPPED = (TOUCH_FILE_HEADER
                     + "input {\n"
                       "    touch {\n"
                       f'        map-to-output "{OUTPUT_NAME}"\n'
                       "    }\n"
                       "}\n")

# A touchscreen reports direct (on-screen) touches and multi-touch
# positions: uinput_touch.INPUT_PROP_DIRECT and ABS_MT_POSITION_X.
SYSFS_INPUT = "/sys/class/input"
# The kernel prints capability bitmaps as space-separated words of its own
# "long" (64 bits on 64-bit kernels), most significant word first.
KERNEL_LONG_BITS = struct.calcsize("l") * 8

INCLUDE_DEPTH_LIMIT = 10        # niri's own limit on nested includes


def log(*a):
    # Never raise: once the terminal or the TUI reading our stderr is gone,
    # a print would throw, and in the event watcher thread that would end
    # the re-applying of our layout after a config reload.
    try:
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# IPC: JSON lines over $NIRI_SOCKET
# --------------------------------------------------------------------------- #

class NiriError(RuntimeError):
    """niri answered a request with {"Err": message}. `message` is niri's
    own text (e.g. "output 'Tandem' not found"); transport problems raise a
    plain RuntimeError instead."""

    def __init__(self, message):
        super().__init__(f"niri: {message}")
        self.message = message


def socket_path():
    """The running niri's IPC socket, or None outside a niri session."""
    return os.environ.get("NIRI_SOCKET") or None


def socket_is_live(path):
    """True if `path` is a UNIX socket that exists. NIRI_SOCKET can outlive
    its niri: `niri --session` exports it to systemd and D-Bus activation,
    and only a clean exit takes it back, so after a crash a GNOME or Plasma
    login can still hand it to its terminals."""
    try:
        return stat.S_ISSOCK(os.stat(path).st_mode)
    except (OSError, TypeError, ValueError):
        return False


def _read_line(sock):
    """Bytes up to (not including) the first newline. Raises RuntimeError
    if the peer closes first."""
    chunks = []
    while True:
        chunk = sock.recv(65536)
        if not chunk:
            raise RuntimeError("niri closed the connection without answering")
        newline = chunk.find(b"\n")
        if newline >= 0:
            chunks.append(chunk[:newline])
            return b"".join(chunks)
        chunks.append(chunk)


def unwrap_reply(line):
    """The value inside niri's {"Ok": value} reply; raises NiriError for
    {"Err": message} and RuntimeError for anything else."""
    try:
        reply = json.loads(line)
    except (ValueError, UnicodeDecodeError) as e:
        raise RuntimeError(f"niri sent a reply Tandem can't read ({e})")
    if isinstance(reply, dict) and len(reply) == 1:
        if "Ok" in reply:
            return reply["Ok"]
        if "Err" in reply:
            raise NiriError(str(reply["Err"]))
    raise RuntimeError(f"niri sent an unexpected reply ({line[:200]!r})")


def request(payload, timeout=IPC_TIMEOUT, path=None):
    """Send one request on a new connection and return the unwrapped reply.

    Raises NiriError when niri refuses it, RuntimeError when niri can't be
    reached or doesn't answer in time.
    """
    path = path or socket_path()
    if not path:
        raise RuntimeError("NIRI_SOCKET is not set (not a niri session)")
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(path)
            sock.sendall(json.dumps(payload).encode() + b"\n")
            line = _read_line(sock)
    except socket.timeout:
        raise RuntimeError(f"niri did not answer within {timeout:g} s")
    except OSError as e:
        raise RuntimeError(f"cannot reach niri at {path} ({e})")
    return unwrap_reply(line)


def query(name, key=None, timeout=IPC_TIMEOUT):
    """A read-only request such as "Outputs"; returns the payload under
    `key` (default: the request name), or None on any failure."""
    try:
        reply = request(name, timeout=timeout)
    except RuntimeError:
        return None
    if not isinstance(reply, dict):
        return None
    return reply.get(key or name)


def outputs():
    """{name: output} as niri reports it, or None if niri can't be asked."""
    result = query("Outputs")
    return result if isinstance(result, dict) else None


def version():
    """niri's version string ("26.04 (8ed0da4)"), or None."""
    result = query("Version")
    return result if isinstance(result, str) else None


def output_action(name, action):
    """Change one setting of output `name` (niri applies it right after
    answering). Raises RuntimeError when niri refuses or doesn't know it."""
    reply = request({"Output": {"output": name, "action": action}})
    if reply != {"OutputConfigChanged": "Applied"}:
        raise RuntimeError(f"niri did not apply the change to {name} "
                           f"({reply})")


def virtual_outputs_supported():
    """True if this niri understands the virtual-output requests, False if
    it doesn't, None if niri can't be asked.

    Asks to remove an output with an empty name, which can't exist: a niri
    with virtual outputs answers "not found", an older one "error parsing
    request". Nothing is changed either way.
    """
    try:
        request({"RemoveVirtualOutput": {"name": ""}})
    except NiriError as e:
        return e.message != UNKNOWN_REQUEST
    except RuntimeError:
        return None
    return True


NEEDS_VIRTUAL_OUTPUTS = ("Tandem needs a niri build with virtual-output "
                         "support (niri pull request #3800, not in a niri "
                         "release yet)")


def _this_niri():
    """'this niri (26.04 (8ed0da4))', or 'this niri' if it won't say."""
    running = version()
    return f"this niri ({running})" if running else "this niri"


def unsupported_message():
    """The error for a niri without virtual outputs, naming its version."""
    return (f"{_this_niri()} cannot create virtual monitors: "
            f"{NEEDS_VIRTUAL_OUTPUTS}")


# --------------------------------------------------------------------------- #
# Outputs: recognising ours, the main monitor, placement
# --------------------------------------------------------------------------- #

def is_virtual(output):
    """True for an output niri created on request (make "niri", model equal
    to its name); real monitors carry their EDID make and model."""
    return (output.get("make") == VIRTUAL_MAKE
            and output.get("model") == output.get("name"))


def is_tandem_output(output):
    """True if `output` is a virtual output named like ours (a leftover of
    a daemon that was killed, or our own)."""
    return output.get("name") == OUTPUT_NAME and is_virtual(output)


def logical_rect(output):
    """(x, y, w, h) in niri's logical coordinates, or None while it's off."""
    logical = output.get("logical")
    if not logical:
        return None
    return (logical["x"], logical["y"], logical["width"], logical["height"])


def current_mode(output):
    """(width, height, refresh in mHz) of the current mode, or None."""
    index = output.get("current_mode")
    modes = output.get("modes") or []
    if index is None or not 0 <= index < len(modes):
        return None
    mode = modes[index]
    return mode["width"], mode["height"], mode["refresh_rate"]


def pick_anchor(all_outputs, own=OUTPUT_NAME):
    """The output the phone is placed against: the user's main monitor.

    niri has no "primary" monitor, so: among the enabled outputs other than
    ours, preferring real monitors over virtual ones, the one at 0,0 (where
    niri starts its layout), else the top-left one. None if there is none.
    """
    enabled = [o for name, o in all_outputs.items()
               if name != own and logical_rect(o)]
    real = [o for o in enabled if not is_virtual(o)]
    candidates = real or enabled
    if not candidates:
        return None
    for output in candidates:
        x, y, w, h = logical_rect(output)
        if x <= 0 < x + w and y <= 0 < y + h:
            return output
    return min(candidates, key=lambda o: logical_rect(o)[:2])


def explicit_position(side, all_outputs, own_size, own=OUTPUT_NAME):
    """Where to put the phone's output for `side`: (x, y), or None for
    niri's automatic placement (always used for "right", and when there is
    no other output to place it beside)."""
    anchor = pick_anchor(all_outputs, own)
    if side == "right" or anchor is None:
        return None
    others = [logical_rect(o) for name, o in all_outputs.items()
              if name != own and logical_rect(o)]
    return place_beside(logical_rect(anchor), others, own_size, side)


def positions_of_others(all_outputs, own=OUTPUT_NAME):
    """{name: (x, y)} of every enabled output except ours."""
    return {name: logical_rect(o)[:2] for name, o in all_outputs.items()
            if name != own and logical_rect(o)}


def moved_outputs(before, after):
    """Names of outputs present in both snapshots whose position changed."""
    return sorted(name for name, position in before.items()
                  if name in after and after[name] != position)


def wait_for_outputs(predicate, timeout):
    """Poll Outputs until predicate(outputs) holds; returns the last
    snapshot (None if niri never answered) and whether it held."""
    deadline = time.monotonic() + timeout
    snapshot = None
    while True:
        latest = outputs()
        if latest is not None:
            snapshot = latest
            if predicate(snapshot):
                return snapshot, True
        if time.monotonic() >= deadline:
            return snapshot, False
        time.sleep(POLL_INTERVAL)


# --------------------------------------------------------------------------- #
# Removing the output (teardown, and a leftover from a killed daemon)
# --------------------------------------------------------------------------- #

def _workspaces_with_windows_on(output_name):
    """Ids of the workspaces on `output_name` that hold a window."""
    workspaces = query("Workspaces") or []
    windows = query("Windows") or []
    with_windows = {w.get("workspace_id") for w in windows}
    return [ws["id"] for ws in workspaces
            if ws.get("output") == output_name
            and (ws["id"] in with_windows
                 or ws.get("active_window_id") is not None)]


def move_windows_off(output_name, target):
    """Move every workspace with windows from `output_name` to `target`.

    niri would rehome them on removal by itself, but it remembers where they
    came from and moves them back when an output of that name returns; moved
    by id (the CLI can only move by index), they stay on the main monitor.
    Returns how many were moved; failures are logged, not raised.
    """
    moved = 0
    for workspace_id in _workspaces_with_windows_on(output_name):
        try:
            request({"Action": {"MoveWorkspaceToMonitor": {
                "output": target, "reference": {"Id": workspace_id}}}})
            moved += 1
        except RuntimeError as e:
            log(f"warning: could not move workspace {workspace_id} to "
                f"{target} ({e})")
    return moved


def remove_output(name=OUTPUT_NAME):
    """RemoveVirtualOutput; True if niri removed it, False if it was already
    gone. Raises RuntimeError for any other failure."""
    try:
        request({"RemoveVirtualOutput": {"name": name}})
    except NiriError as e:
        if "not found" in e.message:
            return False
        raise
    return True


def remove_tandem_output(all_outputs=None):
    """Move the windows off our output and remove it, if it is there.

    Only an output that is ours (virtual, named "Tandem") is touched.
    Returns True if one was removed. Raises RuntimeError if niri refused.
    """
    all_outputs = outputs() if all_outputs is None else all_outputs
    own = (all_outputs or {}).get(OUTPUT_NAME)
    if not own or not is_tandem_output(own):
        return False
    anchor = pick_anchor(all_outputs)
    if anchor:
        moved = move_windows_off(OUTPUT_NAME, anchor["name"])
        if moved:
            log(f"moved {moved} workspace(s) from {OUTPUT_NAME} to "
                f"{anchor['name']}")
    return remove_output(OUTPUT_NAME)


# --------------------------------------------------------------------------- #
# niri config: where tandem.kdl lives and whether config.kdl includes it
# --------------------------------------------------------------------------- #

# Where niri finds its config (niri's src/main.rs, config_path()):
#   1. `niri -c PATH` / `--config PATH`, else $NIRI_CONFIG: exactly that
#      file ("explicit");
#   2. else ${XDG_CONFIG_HOME:-~/.config}/niri/config.kdl, falling back to
#      /etc/niri/config.kdl while the user has none.
# niri removes NIRI_CONFIG from its environment before it starts any
# client, and Tandem can't see a -c argument from its own environment, so
# both are read from the running niri's process (system.py: its PID from
# SO_PEERCRED on NIRI_SOCKET, then /proc/<pid>/cmdline and environ). Only
# when that can't be read (no niri reachable) is Tandem's own environment
# used instead, the best guess left.

class ConfigLocation(NamedTuple):
    path: str               # the config file niri reads, or would create
    explicit: bool          # -c / NIRI_CONFIG: no system-wide fallback


def running_niri_pid(path=None):
    """PID of the niri serving `path` (default NIRI_SOCKET), or None (no
    niri reachable, or one of another user)."""
    return socket_peer_pid(path or socket_path())


def config_argument(argv):
    """The value of niri's -c / --config in `argv`, or None. Arguments
    after "--" are the command niri spawns, not niri's own."""
    arguments = argv[1:]
    for index, argument in enumerate(arguments):
        if argument == "--":
            break
        if argument in ("-c", "--config"):
            return (arguments[index + 1] if index + 1 < len(arguments)
                    else None)
        if argument.startswith("--config="):
            return argument[len("--config="):]
        if argument.startswith("-c") and not argument.startswith("--"):
            value = argument[2:]                    # -cPATH or -c=PATH
            return value[1:] if value.startswith("=") else value
    return None


def config_location_from(argv, environment, cwd):
    """ConfigLocation niri resolves from its argv, environment and working
    directory, by niri's rules (see above)."""
    def absolute(path):
        # niri takes the path as given (a shell expands ~ before niri sees
        # it); relative paths are relative to niri's working directory.
        if not os.path.isabs(path):
            path = os.path.join(cwd or os.getcwd(), path)
        return os.path.normpath(path)

    explicit = config_argument(argv) or environment.get("NIRI_CONFIG")
    if explicit:
        return ConfigLocation(absolute(explicit), True)
    home = environment.get("HOME") or os.path.expanduser("~")
    # Like niri (the directories crate), a relative XDG_CONFIG_HOME is
    # ignored.
    base = environment.get("XDG_CONFIG_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(home, ".config")
    return ConfigLocation(os.path.join(base, "niri", "config.kdl"), False)


def config_location():
    """Where the running niri reads its config; from Tandem's own
    environment when no niri can be asked."""
    pid = running_niri_pid()
    launch = read_process_launch(pid) if pid else None
    if launch:
        return config_location_from(launch.argv, launch.environment,
                                    launch.cwd)
    return config_location_from([], dict(os.environ), os.getcwd())


def user_config_file():
    """The config file the user edits: niri's explicit config, else
    config.kdl in niri's config directory (see config_location())."""
    return config_location().path


def config_dir():
    """The directory of user_config_file(): where tandem.kdl goes, since
    niri resolves a relative include against the including file."""
    return os.path.dirname(user_config_file())


def active_config_file():
    """The file niri reads: the user's, or the system-wide one when the user
    has none (niri falls back to /etc/niri/config.kdl), else None."""
    location = config_location()
    if location.explicit or os.path.exists(location.path):
        return location.path
    if os.path.exists(SYSTEM_CONFIG):
        return SYSTEM_CONFIG
    return None


def touch_file_path():
    """Where Tandem writes its touch mapping (tandem.kdl)."""
    return os.path.join(config_dir(), TOUCH_FILE_NAME)


def _strip_comments(text):
    """KDL text without /* block */ (nested), // line and /- node comments,
    leaving string contents alone. Good enough to find top-level nodes."""
    out = []
    index, depth, in_string = 0, 0, False
    length = len(text)
    while index < length:
        pair = text[index:index + 2]
        char = text[index]
        if depth:
            if pair == "/*":
                depth, index = depth + 1, index + 2
            elif pair == "*/":
                depth, index = depth - 1, index + 2
            else:
                if char == "\n":
                    out.append("\n")      # keep line structure
                index += 1
            continue
        if in_string:
            out.append(char)
            if char == "\\" and index + 1 < length:
                out.append(text[index + 1])
                index += 2
                continue
            if char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            out.append(char)
            index += 1
        elif pair == "/*":
            depth, index = 1, index + 2
        elif pair == "//":
            newline = text.find("\n", index)
            index = length if newline < 0 else newline
        elif pair == "/-":
            # Slashdash comments out the next node: drop the rest of the
            # line, which is all an include node can occupy.
            newline = text.find("\n", index)
            index = length if newline < 0 else newline
        else:
            out.append(char)
            index += 1
    return "".join(out)


# include [optional=true|false] "path"   (top level, one per line)
_INCLUDE_RE = re.compile(
    r'^\s*include\s+(?:optional\s*=\s*(?:true|false)\s+)?'
    r'(?:"((?:[^"\\]|\\.)*)"|r(#*)"(.*?)"\2)\s*'
    r'(?:optional\s*=\s*(?:true|false))?\s*;?\s*$',
    re.MULTILINE)


def include_paths(config_file):
    """Absolute paths of the files `config_file` includes, resolved the way
    niri does (relative to the including file, ~ for home)."""
    try:
        with open(config_file, encoding="utf-8", errors="replace") as f:
            text = f.read()
    except OSError:
        return []
    base = os.path.dirname(os.path.abspath(config_file))
    paths = []
    for match in _INCLUDE_RE.finditer(_strip_comments(text)):
        quoted, _hashes, raw = match.groups()
        path = raw if quoted is None else re.sub(r"\\(.)", r"\1", quoted)
        if path.startswith("~"):
            path = os.path.expanduser(path)
        paths.append(os.path.normpath(os.path.join(base, path)))
    return paths


def config_includes_touch_file(config_file=None, target=None):
    """True if niri's config (or a file it includes) includes tandem.kdl."""
    config_file = config_file or active_config_file()
    target = os.path.realpath(target or touch_file_path())
    if not config_file:
        return False
    pending, seen = [(config_file, 0)], set()
    while pending:
        path, depth = pending.pop()
        real = os.path.realpath(path)
        if real in seen or depth > INCLUDE_DEPTH_LIMIT:
            continue
        seen.add(real)
        for included in include_paths(path):
            if os.path.realpath(included) == target:
                return True
            pending.append((included, depth + 1))
    return False


def include_fix():
    """(problem, fix argv or None) for a config that lacks the include.

    The fix appends the include line to the user's config.kdl (no root
    needed). When niri runs on the system-wide config because the user has
    none, it first copies that one into place, as niri itself would have
    on first start; with no config at all there is nothing safe to append
    to (a file with only the include would replace niri's defaults). A
    config niri was started with (-c / NIRI_CONFIG) that doesn't exist has
    no fix either: niri then runs on its built-in defaults, never on the
    system-wide file, so copying that one there would change the desktop.
    """
    location = config_location()
    user = location.path
    append = (f"printf '\\n%s\\n' {shlex.quote(INCLUDE_LINE)} >> "
              f"{shlex.quote(user)}")
    problem = (f"niri's config needs the line {INCLUDE_LINE} so touches "
               "land on the phone's monitor")
    if os.path.exists(user):
        return problem, ["sh", "-c", append]
    if location.explicit:
        return (f"{problem}; niri was started with the config file {user}, "
                "which doesn't exist (create it first)"), None
    if os.path.exists(SYSTEM_CONFIG):
        script = (f"mkdir -p {shlex.quote(os.path.dirname(user))} && "
                  f"cp {shlex.quote(SYSTEM_CONFIG)} {shlex.quote(user)} && "
                  + append)
        return problem, ["sh", "-c", script]
    return (f"{problem}; niri's config file {user} doesn't exist yet "
            "(start niri once to create it)"), None


def write_touch_mapping(path=None):
    """Map touchscreens to the phone's output (tandem.kdl). Raises OSError."""
    # Atomic: niri polls the file and must never read it half-written.
    write_atomically(path or touch_file_path(), TOUCH_FILE_MAPPED)


def _empty_touch_file(path):
    """Write the cleared tandem.kdl over an existing one that differs.
    Returns True if it was rewritten. Raises OSError.

    The file is kept (only emptied): the include is optional, but niri
    warns about a missing optional include on every config reload, and a
    user who wrote it without optional=true would get a config error.
    """
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            if f.read() == TOUCH_FILE_CLEARED:
                return False
    except FileNotFoundError:
        return False
    write_atomically(path, TOUCH_FILE_CLEARED)
    return True


def clear_touch_mapping(path=None):
    """Empty tandem.kdl again if it holds a mapping. Never raises; returns
    True if the file was changed."""
    path = path or touch_file_path()
    try:
        return _empty_touch_file(path)
    except OSError as e:
        log(f"warning: could not clear the touch mapping in {path} ({e})")
        return False


# --------------------------------------------------------------------------- #
# Touchscreens: niri's mapping is global, so a real one rules touch out
# --------------------------------------------------------------------------- #

def _read_text(path):
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.read().strip()


def parse_bitmap(text, word_bits=None):
    """A sysfs capability bitmap ("1 0 6000000 0", most significant word
    first) as one integer; words are KERNEL_LONG_BITS wide unless
    `word_bits` says otherwise. Raises ValueError."""
    word_bits = word_bits or KERNEL_LONG_BITS
    value = 0
    for word in text.split():
        value = (value << word_bits) | int(word, 16)
    return value


def real_touchscreens(sysfs_input=SYSFS_INPUT):
    """Names of touchscreens on this computer other than Tandem's own.

    A touchscreen is an input device with INPUT_PROP_DIRECT (touches land
    where they are) that reports multi-touch positions. Unreadable entries
    are skipped.
    """
    names = []
    try:
        entries = sorted(e for e in os.listdir(sysfs_input)
                         if e.startswith("event"))
    except OSError:
        return names
    for entry in entries:
        device = os.path.join(sysfs_input, entry, "device")
        try:
            name = _read_text(os.path.join(device, "name"))
            properties = parse_bitmap(
                _read_text(os.path.join(device, "properties")))
            absolute = parse_bitmap(
                _read_text(os.path.join(device, "capabilities", "abs")))
        except (OSError, ValueError):
            continue
        if name == uinput_touch.DEVICE_NAME:
            continue
        if (properties >> uinput_touch.INPUT_PROP_DIRECT) & 1 and \
                (absolute >> uinput_touch.ABS_MT_POSITION_X) & 1:
            names.append(name)
    return names


TOUCHSCREEN_PROBLEM = ("niri maps every touchscreen to one output, so touch "
                       "from the phone is off on computers with a "
                       "touchscreen")


# --------------------------------------------------------------------------- #
# Module hooks (see backends/__init__.py): used without a backend instance
# --------------------------------------------------------------------------- #

def session_confidence():
    """3 inside a niri session (NIRI_SOCKET names a socket that exists); 1
    when only XDG_CURRENT_DESKTOP names niri; else 0. A stale NIRI_SOCKET
    left by a crashed niri doesn't count, so it can't take over a GNOME or
    Plasma session."""
    if socket_is_live(socket_path()):
        return 3
    if "niri" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return 1
    return 0


def session_problem():
    """Why Tandem can't make a monitor in this session (lines for the TUI):
    no niri IPC socket, or a niri without virtual outputs."""
    path = socket_path()
    if not path:
        return ["niri's IPC socket isn't available (NIRI_SOCKET is not set).",
                "Start Tandem from a terminal or launcher inside your niri "
                "session."]
    if not socket_is_live(path):
        return [f"NIRI_SOCKET points at {path}, which isn't a socket that "
                "exists (is niri running?).",
                "Start Tandem from a terminal or launcher inside your niri "
                "session."]
    if virtual_outputs_supported() is False:
        return [f"{_this_niri().capitalize()} cannot create virtual "
                "monitors.", NEEDS_VIRTUAL_OUTPUTS + "."]
    return None


def describe_primary_monitor():
    """'<name>  WxH@Hz  at x,y  scale s' for the main monitor, or None."""
    try:
        all_outputs = outputs()
        anchor = pick_anchor(all_outputs) if all_outputs else None
        if not anchor:
            return None
        width, height, refresh = current_mode(anchor) or (0, 0, 0)
        x, y, _w, _h = logical_rect(anchor)
        scale = anchor["logical"].get("scale", 1)
        return (f"{anchor['name']}  {width}x{height}@{refresh / 1000:.0f}"
                f"  at {x},{y}  scale {scale:g}")
    except Exception:
        return None


def remove_leftover_output(output_name):
    """Remove a "Tandem" output a killed daemon left behind (windows moved
    to the main monitor first) and empty its touch mapping.

    `output_name` is the TUI's --output setting, which names Hyprland's
    headless output; the niri backend always uses OUTPUT_NAME, and only an
    output that is ours (virtual, named "Tandem") is removed. Never raises
    and never logs: it runs in the TUI, under the curses screen.
    """
    try:
        if socket_path():
            remove_tandem_output()
    except Exception:
        pass
    try:
        _empty_touch_file(touch_file_path())
    except Exception:
        pass


def touch_setup_problem():
    """One-time touch setup still missing: the uinput rule (fix with sudo,
    shared with Hyprland), then the include line in niri's config (fix
    without root). None when touch is set up, or can't be (a real
    touchscreen: create_touch_surface() explains that)."""
    try:
        uinput = uinput_setup_problem()
        if uinput:
            return uinput
        if real_touchscreens():
            return None
        if not config_includes_touch_file():
            problem, fix = include_fix()
            return {"problem": problem, "fix": fix}
    except Exception:
        return None
    return None


# --------------------------------------------------------------------------- #
# Event stream: re-apply our settings after a config reload
# --------------------------------------------------------------------------- #

class EventWatcher:
    """Follows niri's event stream and calls `on_change` after a config
    reload, debounced: one burst of saves, one re-apply.

    niri keeps settings made over IPC only until its config is reloaded with
    changed output sections; then Tandem falls back to the config's (or
    niri's default) mode, scale 1 and automatic position. The stream
    replays the current state when it starts, including one ConfigLoaded
    (failed or not, whatever the current config is), which is skipped.

    niri sends no hotplug events, so nothing is re-applied when a monitor
    is plugged in: with the automatic (right) position niri keeps the phone
    at the right edge itself; an explicit position is left as it was.
    """

    def __init__(self, on_change, debounce=0.4):
        self.on_change, self.debounce = on_change, debounce
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run,
                                        name="tandem-niri-events",
                                        daemon=True)
        self._thread.start()

    def stop(self, timeout=1.0):
        """Stop following events; waits briefly for the thread to end."""
        self._stop.set()
        thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self):
        reconnect = False
        while not self._stop.is_set():
            path = socket_path()
            if not path:
                return                      # no niri to follow
            try:
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                    s.settimeout(IPC_TIMEOUT)
                    s.connect(path)
                    s.sendall(b'"EventStream"\n')
                    pending = self._handshake(s)
                    # After a reconnect events may have been missed.
                    self._follow(s, pending, due_now=reconnect)
            except (OSError, RuntimeError) as e:
                if not self._stop.is_set():
                    log(f"warning: lost niri's event stream ({e}); "
                        "retrying")
            reconnect = True
            self._stop.wait(2.0)            # niri restarting: retry later

    @staticmethod
    def _handshake(sock):
        """Read niri's reply to EventStream; returns bytes read past it."""
        buffer = b""
        while b"\n" not in buffer:
            chunk = sock.recv(4096)
            if not chunk:
                raise RuntimeError("niri closed the event stream")
            buffer += chunk
        line, _, rest = buffer.partition(b"\n")
        unwrap_reply(line)                  # raises on {"Err": ...}
        return rest

    @staticmethod
    def config_loaded(line):
        """For a ConfigLoaded event line, whether that config parsed (True)
        or failed (False); None for any other line."""
        try:
            event = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            return None
        loaded = event.get("ConfigLoaded") if isinstance(event, dict) \
            else None
        if not isinstance(loaded, dict):
            return None
        return not loaded.get("failed", False)

    def _follow(self, sock, buffer, due_now=False):
        replay_skipped = False
        due = time.monotonic() if due_now else None
        while not self._stop.is_set():
            # Wake up when the debounce is due, not only when niri writes:
            # otherwise the next event would push back a re-apply that
            # was already due.
            wait = 0.5 if due is None else due - time.monotonic()
            sock.settimeout(min(0.5, max(0.01, wait)))
            try:
                chunk = sock.recv(65536)
                if not chunk:
                    return                  # niri went away
                buffer += chunk
            except socket.timeout:
                pass
            *lines, buffer = buffer.split(b"\n")
            for line in lines:
                parsed = self.config_loaded(line)
                if parsed is None:
                    continue
                if not replay_skipped:
                    # The state replay, not a reload. It says whether the
                    # current config failed, so it is skipped either way:
                    # the next ConfigLoaded is a real reload.
                    replay_skipped = True
                    continue
                if parsed:
                    due = time.monotonic() + self.debounce
            if due is not None and time.monotonic() >= due \
                    and not self._stop.is_set():
                due = None
                try:
                    self.on_change()
                except Exception as e:
                    log(f"warning: could not re-apply {OUTPUT_NAME} ({e})")


# --------------------------------------------------------------------------- #
# Capture: niri's Mutter-compatible ScreenCast D-Bus API
# --------------------------------------------------------------------------- #

class ScreenCastSession:
    """One RecordMonitor session on niri's org.gnome.Mutter.ScreenCast.

    niri offers the interface only in a real session (`niri --session`,
    which display managers use) or with the debug option
    dbus-interfaces-in-non-session-instances.
    """

    def __init__(self, connector):
        self.connector = connector
        self.bus = None
        self.session = None             # D-Bus object path while open
        self.node = None

    def start(self, timeout=STREAM_TIMEOUT):
        """Create the session and start it; returns the PipeWire node id.
        Raises RuntimeError (after closing what it opened)."""
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            (self.session,) = _dbus_call(
                self.bus, SC_BUS, SC_PATH, SC_IFACE, "CreateSession",
                GLib.Variant("(a{sv})", ({},)))
            options = {"cursor-mode": GLib.Variant("u", CURSOR_EMBEDDED)}
            (stream,) = _dbus_call(
                self.bus, SC_BUS, self.session, SC_IFACE + ".Session",
                "RecordMonitor",
                GLib.Variant("(sa{sv})", (self.connector, options)))
            self.node = self._start_and_wait(stream, timeout)
        except Exception as e:
            self.close()
            raise RuntimeError(self._explain(e)) from None
        return self.node

    def _start_and_wait(self, stream, timeout):
        """Start the session; wait for PipeWireStreamAdded on `stream`.

        The signal is delivered on the main context that is the thread's
        default when subscribing, so a private one is pushed for the wait:
        this works from any thread, and nothing else is dispatched by it.
        """
        context = GLib.MainContext.new()
        context.push_thread_default()
        announced = {}

        def on_stream_added(_conn, _sender, _path, _iface, _signal, params):
            announced["node"] = params.unpack()[0]

        subscription = self.bus.signal_subscribe(
            SC_BUS, SC_IFACE + ".Stream", "PipeWireStreamAdded", stream,
            None, Gio.DBusSignalFlags.NONE, on_stream_added)
        try:
            _dbus_call(self.bus, SC_BUS, self.session, SC_IFACE + ".Session",
                       "Start")
            deadline = time.monotonic() + timeout
            while "node" not in announced and time.monotonic() < deadline:
                if not context.iteration(False):
                    time.sleep(0.01)
        finally:
            self.bus.signal_unsubscribe(subscription)
            context.pop_thread_default()
        if "node" not in announced:
            raise RuntimeError("niri never announced the PipeWire stream")
        return announced["node"]

    def _explain(self, error):
        text = getattr(error, "message", None) or str(error)
        if "ServiceUnknown" in str(error) or "was not provided" in text:
            return ("niri's screencast D-Bus service isn't running (niri "
                    "offers it when started as a session, `niri --session`, "
                    "which display managers do)")
        if "no such monitor" in text:
            return f"niri has no output named {self.connector} to record"
        return f"niri's screencast failed ({text})"

    def close(self):
        """Stop the session (niri ends the PipeWire stream). Idempotent."""
        session, self.session = self.session, None
        if self.bus and session:
            try:
                _dbus_call(self.bus, SC_BUS, session, SC_IFACE + ".Session",
                           "Stop")
            except Exception:
                pass                    # already closed (output removed)


# --------------------------------------------------------------------------- #
# backend
# --------------------------------------------------------------------------- #

class NiriBackend:
    name = "niri"
    # niri keeps a virtual output after its creator exits, but restarts of
    # the daemon recreate it anyway (it is cheap and keeps no state), so
    # --keep-output doesn't keep it: teardown() always runs and moves the
    # windows back.
    honors_keep_output = False

    def __init__(self, args):
        self.a = args
        self.proc = None                # gst-launch while capturing
        self.cast = None                # ScreenCastSession while capturing
        self._created = False
        self._torn_down = False
        self._touch_mapped = False
        # What we last asked niri for: None (nothing yet), AUTOMATIC or
        # (x, y). Reset after a config reload, which may drop it, so the
        # position is then sent again even if it looks right.
        self._sent_position = None
        self._placed_side = getattr(args, "side", "right")  # for the log
        # apply_live (control thread), the event watcher's re-apply and
        # teardown (main thread): one layout change at a time.
        self._layout_lock = threading.Lock()
        self._watcher = EventWatcher(self._reapply)

    @staticmethod
    def missing():
        missing = missing_pipeline_tools()
        if Gio is None:
            missing.append("python-gobject (gi)")
        return missing

    # -- virtual output ----------------------------------------------------- #

    def setup(self):
        """Create the "Tandem" output, apply mode / scale / side, and start
        following config reloads. Cleans up after itself when it fails."""
        a = self.a
        if not socket_path():
            raise RuntimeError("NIRI_SOCKET is not set: start Tandem from "
                               "inside your niri session")
        if not (0 < a.width <= 0xFFFF and 0 < a.height <= 0xFFFF):
            raise RuntimeError(f"niri can't create a {a.width}x{a.height} "
                               "output (each side must be 1-65535 pixels)")
        before = outputs()
        if before is None:
            raise RuntimeError("could not read niri's outputs over "
                               f"{socket_path()}")
        if OUTPUT_NAME in before:
            if not is_tandem_output(before[OUTPUT_NAME]):
                raise RuntimeError(f"niri already has an output named "
                                   f"{OUTPUT_NAME} that Tandem didn't make")
            log(f"removing the {OUTPUT_NAME} output a previous daemon left "
                "behind")
            remove_tandem_output(before)
            before, _ = wait_for_outputs(
                lambda now: OUTPUT_NAME not in now, APPEAR_TIMEOUT)
            before = before or {}
        clear_touch_mapping()           # a killed daemon's mapping
        reference = positions_of_others(before)

        self._create()
        try:
            with self._layout_lock:
                self._apply_layout(a.side, a.scale, reference=reference)
        except Exception:
            self._remove_quietly()
            raise
        self._watcher.start()

    def _create(self):
        a = self.a
        log(f"creating niri virtual output {OUTPUT_NAME} {a.width}x"
            f"{a.height}@{a.fps}")
        try:
            request({"CreateVirtualOutput": {
                "width": a.width, "height": a.height,
                "refresh_rate": int(a.fps or 60), "name": OUTPUT_NAME}})
        except NiriError as e:
            if e.message == UNKNOWN_REQUEST:
                raise RuntimeError(unsupported_message()) from None
            raise RuntimeError("niri could not create the virtual output "
                               f"({e.message})") from None
        self._created = True
        _, appeared = wait_for_outputs(
            lambda now: OUTPUT_NAME in now
            and logical_rect(now[OUTPUT_NAME]) is not None, APPEAR_TIMEOUT)
        if not appeared:
            self._remove_quietly()
            raise RuntimeError(f"niri created {OUTPUT_NAME} but never turned "
                               "it on (is it set to `off` in config.kdl?)")

    def _remove_quietly(self):
        """Remove our output after a failed setup (logged, never raised)."""
        try:
            remove_output(OUTPUT_NAME)
        except RuntimeError as e:
            log(f"warning: could not remove {OUTPUT_NAME} ({e})")
        self._created = False

    def _apply_layout(self, side, scale, reference=None):
        """Mode, scale, then position of our output; caller holds the lock.

        Sends only what differs from what niri reports. `reference` is where
        the other outputs were before our output changed (setup passes the
        layout from before the output existed); by default, the current
        layout. Returns True if anything was sent. Raises RuntimeError when
        niri can't be asked or our output is gone.
        """
        a = self.a
        changed = False
        snapshot = outputs()
        if snapshot is None:
            raise RuntimeError("could not read niri's outputs")
        own = snapshot.get(OUTPUT_NAME)
        if own is None or logical_rect(own) is None:
            raise RuntimeError(f"the {OUTPUT_NAME} output is gone")
        if reference is None:
            reference = positions_of_others(snapshot)

        refresh_mhz = int(a.fps or 60) * 1000
        mode = current_mode(own)
        scale = scale or 1.0
        mode_wrong = (not mode or mode[:2] != (a.width, a.height)
                      or abs(mode[2] - refresh_mhz) > 1000)
        scale_wrong = (abs(own["logical"].get("scale", 1.0) - scale)
                       > SCALE_TOLERANCE)
        if (mode_wrong or scale_wrong) and self._sent_position not in \
                (None, AUTOMATIC):
            # An explicit position fits the old size only: at the new size
            # it could overlap the main monitor, and niri would push the
            # user's (automatic) monitors aside. Go automatic first; the
            # new position is computed from that layout below.
            self._send_position(None)
            changed = True
        if mode_wrong:
            # A user `output "Tandem" { mode ... }` block would otherwise
            # decide the size, and the phone's stream would not fit.
            given = f" (niri gave it {mode[0]}x{mode[1]})" if mode else ""
            log(f"setting {OUTPUT_NAME} to {a.width}x{a.height}@{a.fps}"
                + given)
            output_action(OUTPUT_NAME, {"CustomMode": {"mode": {
                "width": a.width, "height": a.height,
                "refresh": float(a.fps or 60)}}})
            changed = True
        if scale_wrong:
            output_action(OUTPUT_NAME, {"Scale": {"scale": {
                "Specific": scale}}})
            changed = True
        if changed:
            snapshot, settled = wait_for_outputs(
                lambda now: self._mode_and_scale_settled(now, scale),
                SETTLE_TIMEOUT)
            if not settled:
                log(f"warning: niri did not apply {a.width}x{a.height} at "
                    f"scale {scale:g} to {OUTPUT_NAME}")
            if snapshot is None or OUTPUT_NAME not in snapshot:
                raise RuntimeError(f"the {OUTPUT_NAME} output is gone")
        if self._place(side, snapshot, reference):
            changed = True
        if changed:
            log(f"placed {OUTPUT_NAME} side={self._placed_side} "
                f"scale={scale:g}")
        return changed

    def _mode_and_scale_settled(self, snapshot, scale):
        own = snapshot.get(OUTPUT_NAME)
        if not own or logical_rect(own) is None:
            return False
        mode = current_mode(own)
        return (bool(mode) and mode[:2] == (self.a.width, self.a.height)
                and abs(own["logical"].get("scale", 1.0) - scale)
                <= SCALE_TOLERANCE)

    def _place(self, side, snapshot, reference):
        """Put our output on `side`; returns True if a change was sent.

        Falls back to the automatic (right) position when niri would move
        the user's monitors for an explicit one, or ignored it (overlap).
        """
        own_size = logical_rect(snapshot[OUTPUT_NAME])[2:]
        target = explicit_position(side, snapshot, own_size)
        current = logical_rect(snapshot[OUTPUT_NAME])[:2]
        wanted = AUTOMATIC if target is None else target
        displaced = moved_outputs(reference, positions_of_others(snapshot))
        if self._sent_position == wanted and not displaced and \
                (target is None or current == target):
            return False

        self._send_position(target)
        if target is None:
            after, _ = self._wait_until_stable()
        else:
            after, _ = wait_for_outputs(
                lambda now: OUTPUT_NAME in now
                and logical_rect(now[OUTPUT_NAME]) is not None
                and logical_rect(now[OUTPUT_NAME])[:2] == target,
                SETTLE_TIMEOUT)
        if after is None or OUTPUT_NAME not in after:
            raise RuntimeError(f"the {OUTPUT_NAME} output is gone")

        moved = moved_outputs(reference, positions_of_others(after))
        landed = logical_rect(after[OUTPUT_NAME])[:2]
        if target is not None and (moved or landed != target):
            reason = (f"niri moved {', '.join(moved)} to make room"
                      if moved else "niri placed it elsewhere (overlap)")
            log(f"warning: could not put {OUTPUT_NAME} {side_phrase(side)} "
                f"the main monitor ({reason}); putting it on the right "
                "instead. Give "
                "your monitors explicit positions in niri's config.kdl "
                "(output { position x=... y=... }) to use other sides")
            self._send_position(None)
            after, _ = self._wait_until_stable()
            moved = (moved_outputs(reference, positions_of_others(after))
                     if after is not None else [])
        automatic = self._sent_position == AUTOMATIC
        if moved and automatic:
            # Rare: niri ordered our output before a monitor it places
            # automatically too (it sorts them by make and model).
            log(f"warning: niri moved {', '.join(moved)} when it placed "
                f"{OUTPUT_NAME}; give your monitors explicit positions in "
                "niri's config.kdl to keep them in place")
        self._placed_side = "right" if automatic else side
        return True

    def _send_position(self, target):
        """Explicit (x, y), or None for niri's automatic placement."""
        if target is None:
            position = "Automatic"
        else:
            position = {"Specific": {"x": target[0], "y": target[1]}}
        output_action(OUTPUT_NAME, {"Position": {"position": position}})
        self._sent_position = AUTOMATIC if target is None else target

    @staticmethod
    def _wait_until_stable():
        """Outputs once two reads POLL_INTERVAL apart agree (an automatic
        position has no target to wait for)."""
        previous = None
        deadline = time.monotonic() + SETTLE_TIMEOUT
        while True:
            time.sleep(POLL_INTERVAL)
            latest = outputs()
            if latest is not None and latest == previous:
                return latest, True
            previous = latest if latest is not None else previous
            if time.monotonic() >= deadline:
                return previous, False

    def _reapply(self):
        """After a config reload (event watcher thread)."""
        with self._layout_lock:
            if self._torn_down:
                return
            self._sent_position = None      # the reload may have dropped it
            self._apply_layout(self.a.side, self.a.scale)

    def apply_live(self, side, scale):
        """New side / scale from the TUI, without restarting capture."""
        with self._layout_lock:
            if self._torn_down:
                return False
            try:
                self._apply_layout(side, scale)
            except RuntimeError as e:
                log(f"warning: could not place {OUTPUT_NAME} ({e})")
                return False
        return True

    # -- capture ------------------------------------------------------------ #

    def start_capture(self):
        """Record our output and start the encoder; returns its H.264 stream.

        Safe to call again after stop_capture() (or after gst-launch exited
        on its own): each call opens a new screencast session.
        """
        a = self.a
        encoder = choose_encoder(a, "niri")
        self.stop_capture()                 # never two encoders on one node
        if Gio is None:
            raise RuntimeError("python-gobject (gi) is missing")
        current = outputs()
        if current is not None and OUTPUT_NAME not in current:
            raise RuntimeError(f"the niri virtual output {OUTPUT_NAME} is "
                               "gone (removed with niri msg, or niri "
                               "restarted)")
        if current is not None and not self._mode_and_scale_settled(
                current, a.scale or 1.0):
            # A config reload just reset our mode (which is also what ended
            # the previous encoder: its caps no longer matched). Restore it
            # now instead of racing the event watcher's debounced re-apply.
            with self._layout_lock:
                if not self._torn_down:
                    self._apply_layout(a.side, a.scale)
        cast = ScreenCastSession(OUTPUT_NAME)
        node = cast.start()
        log(f"niri screencast of {OUTPUT_NAME} on pipewire node {node}")
        try:
            proc, stream = start_encoder(node, a, encoder)
        except Exception:
            cast.close()
            raise
        self.proc, self.cast = proc, cast
        return stream

    def stop_capture(self):
        """Stop the encoder, then the screencast session (the output stays).
        Idempotent."""
        proc, self.proc = self.proc, None
        stop_pipeline(proc)
        cast, self.cast = self.cast, None
        if cast:
            cast.close()

    # -- touch -------------------------------------------------------------- #

    def create_touch_surface(self):
        """A uinput touchscreen mapped to our output through tandem.kdl."""
        problem = uinput_touch.uinput_access_problem()
        if problem:
            raise RuntimeError(f"{problem}; run tandem on the computer and "
                               "press t to set up touch")
        touchscreens = real_touchscreens()
        if touchscreens:
            raise RuntimeError(TOUCHSCREEN_PROBLEM)
        if not config_includes_touch_file():
            raise RuntimeError("niri's config doesn't include tandem.kdl yet; "
                               "run tandem on the computer and press t to "
                               "set up touch")
        path = touch_file_path()
        try:
            write_touch_mapping(path)
        except OSError as e:
            raise RuntimeError(f"could not write {path} ({e})") from None
        self._touch_mapped = True
        try:
            screen = uinput_touch.VirtualTouchscreen(self.a.width,
                                                     self.a.height)
        except Exception:
            clear_touch_mapping(path)
            self._touch_mapped = False
            raise
        screen.description += f" mapped to {OUTPUT_NAME} ({path})"
        return screen

    # -- teardown ----------------------------------------------------------- #

    def teardown(self):
        """Move the windows back, remove the output, empty tandem.kdl.

        Idempotent and never raises (tandemd calls it in its `finally`).
        """
        self._watcher.stop()
        try:
            self.stop_capture()
        except Exception as e:
            log(f"warning: stopping capture failed ({e})")
        with self._layout_lock:
            if self._torn_down:
                return
            self._torn_down = True
            if self._created:
                try:
                    snapshot = outputs()
                    if snapshot is None:
                        raise RuntimeError("niri does not answer")
                    if remove_tandem_output(snapshot):
                        log(f"removed niri virtual output {OUTPUT_NAME}")
                except Exception as e:
                    log(f"warning: could not remove {OUTPUT_NAME} ({e})")
                self._created = False
            if self._touch_mapped:
                clear_touch_mapping()
                self._touch_mapped = False
