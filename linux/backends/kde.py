"""
kde.py - KDE Plasma (KWin, Wayland) backend: Arch / CachyOS, Fedora KDE,
Kubuntu, KDE neon, Debian, openSUSE, any Plasma 6 (5.27 should work too).

KWin can create a *virtual output* and stream it to PipeWire through its
zkde_screencast_unstable_v1 Wayland protocol, and inject touches through
org_kde_kwin_fake_input. Both are Wayland requests, so a small C client,
tandem-kwin-helper (linux/kwin-helper/, built on first use by
kwin_helper.py), does that side and is driven over stdin/stdout:

    tandem-kwin-helper --name Tandem --width W --height H
        -> "ready ...", "created <node>"   KWin made "Virtual-Tandem"
    kscreen-doctor output.Virtual-Tandem.scale.S \
                   output.Virtual-Tandem.position.X,Y
        -> placed beside the main monitor and scaled
    gst-launch-1.0 pipewiresrc path=<node> ! ... ! fdsink   (gstreamer.py)
        -> raw Annex-B on a pipe -> tandemd's parser (same as GNOME)
    "down 0 0.25 0.5" on the helper's stdin
        -> a touch at that fraction of the phone's monitor

The output exists exactly as long as the helper's Wayland connection: when
the daemon stops (or dies: the helper gets SIGTERM from the kernel and EOF on
stdin), KWin removes it and moves its windows back by itself.

Things KWin does that shape this file (measured on KWin 6.7.5 and 6.8.2):
* The output is requested at scale 1 and scaled afterwards: KWin's virtual
  backend multiplies the requested size by the scale, and KWin re-applies a
  scale and position it saved for an output of that name anyway.
* KWin refuses negative positions. Putting the phone left of / above the
  main monitor therefore moves the user's monitors right / down by the same
  amount, keeping their modes, scales, rotations and arrangement (the same
  approach as GNOME's ApplyMonitorsConfig path). The layout's top-left is
  kept at 0,0, so moving the phone back undoes that shift.
* KWin 6.7 and older only offer the two protocols to a program named in a
  desktop entry (kwin_helper.ensure_desktop_entry writes it).
* KWin 6.7 (bug 527747): after the output is disabled (Meta+P "built-in
  only", kscreen-doctor ...disable), every later output of that name fails
  with "Could not find output" (translated into the session's language).
  setup() then retries as Tandem-2, Tandem-3... on any KWin refusal.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import NamedTuple

from . import kwin_helper
from .gstreamer import (choose_encoder, missing_pipeline_tools,
                        start_encoder, stop_pipeline)
from .layout import place_beside, side_phrase, valid_scale

# The helper's --name. KWin calls the output "Virtual-<name>".
OUTPUT_NAME = "Tandem"
OUTPUT_PREFIX = "Virtual-"
# Names tried in turn when KWin 6.7 refuses a name it remembers as disabled
# (bug 527747): Tandem, Tandem-2 ... Tandem-5.
MAX_NAME_ATTEMPTS = 5
OUR_OUTPUT = re.compile(rf"^{OUTPUT_PREFIX}{OUTPUT_NAME}(-\d+)?$")

CREATE_TIMEOUT = 10.0      # s for KWin to answer the helper's request
LISTED_TIMEOUT = 3.0       # s for kscreen-doctor to list a new output
SETTLE_TIMEOUT = 1.5       # s for KWin to apply a placement
PERMISSION_RETRY_DELAY = 2.0   # s for KWin to notice a new desktop entry
KSCREEN_TIMEOUT = 10       # s per kscreen-doctor run
HELPER_STOP_TIMEOUT = 2.0  # s for the helper to exit after stdin EOF

# kscreen-doctor's rotation values (libkscreen Output::Rotation) that turn
# the output by 90 or 270 degrees: Left, Right, Flipped90, Flipped270.
ROTATED_SIDEWAYS = (2, 8, 32, 128)
# kscreen-doctor's priority of the primary monitor.
PRIMARY_PRIORITY = 1


def log(*a):
    # Never raise: once the terminal or the TUI reading our stderr is gone,
    # a print would throw, and in the helper's reader thread that would stop
    # draining its stdout until it blocks.
    try:
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


def _desktop_names():
    """XDG_CURRENT_DESKTOP as lowercase names ("KDE" on Plasma)."""
    return [name.strip().lower()
            for name in os.environ.get("XDG_CURRENT_DESKTOP", "").split(":")]


def is_kde():
    return "kde" in _desktop_names()


# --------------------------------------------------------------------------- #
# kscreen-doctor: reading the layout
# --------------------------------------------------------------------------- #

class KscreenOutput(NamedTuple):
    """One output as `kscreen-doctor -j` reports it. Positions are logical
    (compositor) coordinates; width/height are the mode's pixels."""
    name: str
    enabled: bool
    connected: bool
    x: int
    y: int
    width: int
    height: int
    scale: float
    rotation: int
    priority: int
    refresh: float          # Hz of the current mode, 0 if unknown

    def logical_size(self, scale=None):
        """(w, h) in logical pixels at `scale` (default: its own)."""
        scale = scale or self.scale or 1.0
        width, height = self.width, self.height
        if self.rotation in ROTATED_SIDEWAYS:
            width, height = height, width
        return int(round(width / scale)), int(round(height / scale))

    def rect(self):
        return (self.x, self.y, *self.logical_size())

    @property
    def active(self):
        """Part of the desktop (connected, and enabled)."""
        return self.enabled and self.connected


def _current_refresh(raw):
    mode_id = str(raw.get("currentModeId", ""))
    for mode in raw.get("modes") or []:
        if str(mode.get("id")) == mode_id:
            try:
                return float(mode.get("refreshRate") or 0)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def parse_outputs(text):
    """KscreenOutput list from `kscreen-doctor -j` output; [] if unreadable.

    A missing "enabled" key means enabled: KWin 6.8's virtual outputs don't
    report it (they can't be disabled). Outputs without a size (disabled
    ones may have no current mode) are left out.
    """
    try:
        document = json.loads(text)
    except (TypeError, ValueError):
        return []
    outputs = []
    for raw in (document.get("outputs") if isinstance(document, dict)
                else None) or []:
        try:
            size = raw.get("size") or {}
            position = raw.get("pos") or {}
            if not size.get("width") or not size.get("height"):
                continue
            outputs.append(KscreenOutput(
                name=str(raw["name"]),
                enabled=raw.get("enabled", True) is not False,
                connected=raw.get("connected", True) is not False,
                x=int(position.get("x", 0)),
                y=int(position.get("y", 0)),
                width=int(size["width"]),
                height=int(size["height"]),
                scale=float(raw.get("scale") or 1.0),
                rotation=int(raw.get("rotation") or 1),
                priority=int(raw.get("priority") or 0),
                refresh=_current_refresh(raw)))
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
    return outputs


def query_outputs():
    """The current outputs from `kscreen-doctor -j`, or [] on any failure."""
    try:
        result = subprocess.run(["kscreen-doctor", "-j"], capture_output=True,
                                text=True, timeout=KSCREEN_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return []
    if result.returncode != 0:
        return []
    return parse_outputs(result.stdout)


def primary_output(outputs, exclude=None):
    """The user's main monitor among active `outputs` (not `exclude`): the
    primary (priority 1), else the top-left one; None if there is none."""
    candidates = [output for output in outputs
                  if output.active and output.name != exclude
                  and not OUR_OUTPUT.match(output.name)]
    if not candidates:
        return None
    return next((output for output in candidates
                 if output.priority == PRIMARY_PRIORITY),
                min(candidates, key=lambda output: (output.x, output.y)))


# --------------------------------------------------------------------------- #
# Placement planning (pure: unit-tested with recorded kscreen-doctor JSON)
# --------------------------------------------------------------------------- #

class LayoutPlan(NamedTuple):
    scale: float            # the scale our output gets (snapped)
    positions: dict         # output name -> (x, y) after the change
    arguments: list         # kscreen-doctor arguments; [] = nothing to do
    shifted: tuple          # (dx, dy) applied to the user's monitors


def format_scale(scale):
    """A scale as kscreen-doctor takes it: enough digits that the logical
    size it implies comes out whole (1.333333, not 1.33333)."""
    return f"{scale:.6f}".rstrip("0").rstrip(".")


def plan_layout(outputs, own_name, side, scale):
    """Where everything goes with our output `own_name` on `side` of the
    main monitor at `scale`; see LayoutPlan.

    The scale is snapped so the logical size is whole pixels (KWin, like
    Hyprland, works in 1/120 steps and rounds sizes otherwise, which would
    leave a gap or an overlap beside the main monitor). The anchor is the
    primary monitor, else the top-left one. The final layout's top-left is
    0,0: KWin rejects negative positions, so for left/up every other
    monitor moves by the same offset, keeping its mode, scale, rotation and
    place relative to the others. Our output is never left primary.
    Raises RuntimeError if `own_name` is not among `outputs`.
    """
    own = next((output for output in outputs if output.name == own_name),
               None)
    if own is None:
        raise RuntimeError(f"{own_name} is not in KWin's output list")
    snapped = valid_scale(own.width, own.height, scale or own.scale or 1.0)
    own_size = own.logical_size(snapped)

    others = [output for output in outputs
              if output.active and output.name != own_name]
    anchor = primary_output(outputs, exclude=own_name)
    if anchor is None:
        target = (0, 0)
    else:
        target = place_beside(anchor.rect(), [o.rect() for o in others],
                              own_size, side)

    positions = {output.name: (output.x, output.y) for output in others}
    positions[own_name] = target
    left = min(x for x, _y in positions.values())
    top = min(y for _x, y in positions.values())
    positions = {name: (x - left, y - top)
                 for name, (x, y) in positions.items()}
    shift = (-left, -top) if others else (0, 0)

    arguments = []
    # The user's monitors first: their new positions make room for ours.
    for output in others:
        if positions[output.name] != (output.x, output.y):
            x, y = positions[output.name]
            arguments.append(f"output.{output.name}.position.{x},{y}")
    if abs(snapped - own.scale) > 1e-4:
        arguments.append(f"output.{own_name}.scale.{format_scale(snapped)}")
    if positions[own_name] != (own.x, own.y):
        x, y = positions[own_name]
        arguments.append(f"output.{own_name}.position.{x},{y}")
    if own.priority == PRIMARY_PRIORITY and anchor is not None:
        # Never leave the main monitor's role (panels, new windows) on the
        # phone: hand it back to the anchor.
        arguments.append(f"output.{anchor.name}.priority.{PRIMARY_PRIORITY}")
    return LayoutPlan(snapped, positions, arguments, shift)


def layout_matches(outputs, plan, own_name):
    """True if `outputs` (read back from KWin) are where `plan` put them."""
    by_name = {output.name: output for output in outputs}
    own = by_name.get(own_name)
    if own is None or abs(own.scale - plan.scale) > 1e-3:
        return False
    return all(name in by_name and (by_name[name].x, by_name[name].y) == pos
               for name, pos in plan.positions.items())


# What kscreen-doctor prints when it applied nothing ("applying config
# failed! Position of enabled output ... is negative", "Output with name or
# uuid X not found."). It exits 0 in both cases, so the text is the signal.
KSCREEN_FAILURE_MARKERS = ("failed", "not found")


def run_kscreen_doctor(arguments):
    """Apply `arguments` in one kscreen-doctor run; raise RuntimeError with
    its message when KWin rejects them."""
    try:
        result = subprocess.run(["kscreen-doctor", *arguments],
                                capture_output=True, text=True,
                                timeout=KSCREEN_TIMEOUT)
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"kscreen-doctor did not run ({error})") from error
    output = f"{result.stdout}\n{result.stderr}"
    if (result.returncode != 0
            or any(marker in output.lower()
                   for marker in KSCREEN_FAILURE_MARKERS)):
        detail = " | ".join(line.strip() for line in output.splitlines()
                            if line.strip())[-300:]
        raise RuntimeError(f"kscreen-doctor {' '.join(arguments)} failed "
                           f"({detail or f'exit status {result.returncode}'})")


# --------------------------------------------------------------------------- #
# The helper process
# --------------------------------------------------------------------------- #

def parse_event(line):
    """One helper stdout line -> (kind, value), or None if malformed.

    ("ready", (screencast_version, fake_input_version)), ("created", node),
    ("geometry", (x, y, w, h)), ("failed", reason), ("closed", None),
    ("error", text).
    """
    kind, _space, rest = line.strip().partition(" ")
    try:
        if kind == "ready":
            fields = dict(field.split("=", 1) for field in rest.split())
            return kind, (int(fields["screencast"]), int(fields["fake_input"]))
        if kind == "created":
            return kind, int(rest)
        if kind == "geometry":
            values = tuple(int(value) for value in rest.split())
            return (kind, values) if len(values) == 4 else None
        if kind in ("failed", "error"):
            return kind, rest.strip()
        if kind == "closed" and not rest:
            return kind, None
    except (KeyError, ValueError):
        return None
    return None


class KwinHelper:
    """A running tandem-kwin-helper: its stdout events kept as state (read
    by a thread), its stdin the channel for touch commands.

    State fields are written by the reader thread under `_changed` and read
    by anyone; wait_for_outcome() waits on them.
    """

    def __init__(self, binary, name, width, height):
        self.name = name
        self.output_name = OUTPUT_PREFIX + name
        self.screencast_version = None     # None until "ready"
        self.fake_input_version = 0
        self.node = None
        self.geometry = None               # (x, y, w, h), logical
        self.failure = None                # KWin's reason from "failed"
        self.error = None                  # the helper's own "error"
        self.closed = False                # KWin ended the stream
        self.exited = False                # stdout reached EOF
        self._changed = threading.Condition()
        self._write_lock = threading.Lock()
        self._stdin_open = True
        try:
            self._proc = subprocess.Popen(
                [binary, "--name", name, "--width", str(width),
                 "--height", str(height)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                # Its own session: a Ctrl-C on the daemon's terminal is the
                # daemon's to handle (it closes our stdin when it stops).
                start_new_session=True)
        except OSError as error:
            raise RuntimeError(f"could not start {binary} ({error})") \
                from error
        threading.Thread(target=self._read_events, name="tandem-kwin-events",
                         daemon=True).start()
        threading.Thread(target=self._forward_stderr, name="tandem-kwin-log",
                         daemon=True).start()

    # -- reader threads ----------------------------------------------------- #

    def _read_events(self):
        try:
            for raw in self._proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if line:
                    self._apply(line)
        except (OSError, ValueError):
            pass                            # pipe closed under us at stop()
        finally:
            try:
                self._proc.stdout.close()   # don't leak a pipe per helper
            except OSError:
                pass
            with self._changed:
                self.exited = True
                self._changed.notify_all()

    def _apply(self, line):
        event = parse_event(line)
        if event is None:
            log(f"warning: unexpected line from the KWin helper: {line}")
            return
        kind, value = event
        with self._changed:
            if kind == "ready":
                self.screencast_version, self.fake_input_version = value
            elif kind == "created":
                self.node = value
            elif kind == "geometry":
                self.geometry = value
            elif kind == "failed":
                self.failure = value
            elif kind == "error":
                self.error = value
            elif kind == "closed":
                self.closed = True
            self._changed.notify_all()
        if kind == "closed" and self.node is not None:
            log(f"warning: KWin closed {self.output_name} (it was disabled, "
                "or KWin restarted)")
        elif kind == "error":
            log(f"error: KWin helper: {value}")

    def _forward_stderr(self):
        """The helper's notes (ignored commands and such) go to our log."""
        try:
            for raw in self._proc.stderr:
                line = raw.decode("utf-8", "replace").rstrip()
                if line:
                    log(line if line.startswith("tandem-kwin-helper")
                        else f"tandem-kwin-helper: {line}")
        except (OSError, ValueError):
            pass
        finally:
            try:
                self._proc.stderr.close()
            except OSError:
                pass

    # -- state -------------------------------------------------------------- #

    @property
    def refused_screencast(self):
        """KWin answered but didn't offer the screencast protocol to us."""
        return self.screencast_version == 0

    def wait_for_outcome(self, timeout):
        """Wait until KWin created the output or something ended the
        attempt (failed, closed, helper error or exit, protocol refused).
        Returns True if the output was created."""
        def decided():
            return (self.node is not None or self.failure is not None
                    or self.error is not None or self.closed or self.exited
                    or self.refused_screencast)
        with self._changed:
            self._changed.wait_for(decided, timeout)
            return self.node is not None and not self.closed

    @property
    def alive(self):
        """The output still exists (created, not closed, helper running)."""
        return (self.node is not None and not self.closed and not self.exited
                and self._proc.poll() is None)

    def exit_status(self):
        return self._proc.poll()

    # -- commands ----------------------------------------------------------- #

    def send(self, command):
        """Write one command line. Raises RuntimeError once the helper is
        gone (or stop() ran)."""
        with self._write_lock:
            if not self._stdin_open:
                raise RuntimeError("the KWin helper has stopped")
            try:
                self._proc.stdin.write(command.encode("ascii") + b"\n")
                self._proc.stdin.flush()
            except (OSError, ValueError) as error:
                raise RuntimeError(f"the KWin helper has stopped ({error})") \
                    from error

    def stop(self):
        """Close its stdin (it closes the stream and exits), then make sure
        it is gone: SIGTERM after HELPER_STOP_TIMEOUT, SIGKILL after one
        more second. Idempotent."""
        with self._write_lock:
            if self._stdin_open:
                self._stdin_open = False
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass                    # already gone: nothing to flush
        if self._proc.poll() is not None:
            return
        try:
            self._proc.wait(timeout=HELPER_STOP_TIMEOUT)
            return
        except subprocess.TimeoutExpired:
            log("warning: the KWin helper did not exit; terminating it")
        self._proc.terminate()
        try:
            self._proc.wait(timeout=1.0)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()


# --------------------------------------------------------------------------- #
# Touch input (KWin fake input, through the helper)
# --------------------------------------------------------------------------- #

def _fraction(value):
    return f"{min(max(float(value), 0.0), 1.0):.6f}"


class KwinTouchscreen:
    """Touches sent as helper commands; KWin's fake input delivers them as
    real touch events on the phone's monitor (the mouse pointer stays put).

    Coordinates are fractions of the monitor; the helper turns them into
    KWin's global coordinates from the output's live position, so this
    keeps working after the monitor moved or was rescaled.
    """

    slot_count = 10

    def __init__(self, helper):
        self.description = f"KWin fake input on {helper.output_name}"
        self._helper = helper
        self._active_slots = set()
        self._closed = False

    def down(self, slot, x, y):
        if self._closed:
            return
        self._active_slots.add(slot)
        self._helper.send(f"down {int(slot)} {_fraction(x)} {_fraction(y)}")

    def move(self, slot, x, y):
        if self._closed or slot not in self._active_slots:
            return
        self._helper.send(f"move {int(slot)} {_fraction(x)} {_fraction(y)}")

    def up(self, slot):
        if self._closed or slot not in self._active_slots:
            return
        self._active_slots.discard(slot)
        self._helper.send(f"up {int(slot)}")

    def release_all(self):
        for slot in sorted(self._active_slots):
            self.up(slot)

    def close(self):
        """Lift every finger still down. Idempotent; a helper that is
        already gone needs nothing (KWin cancels its touches itself)."""
        if self._closed:
            return
        try:
            self.release_all()
        except RuntimeError:
            self._active_slots.clear()
        self._closed = True


# --------------------------------------------------------------------------- #
# Module hooks (see backends/__init__.py): used without a backend instance
# --------------------------------------------------------------------------- #

def session_confidence():
    """2 in a Plasma Wayland session; 1 when XDG_CURRENT_DESKTOP names KDE
    without Wayland (X11: setup() fails, session_problem() explains); else
    0."""
    if not is_kde():
        return 0
    return 2 if os.environ.get("WAYLAND_DISPLAY") else 1


def session_problem():
    """Why this session can't host a virtual monitor (lines for the TUI)."""
    if os.environ.get("WAYLAND_DISPLAY"):
        return None
    return ["Plasma is running on X11 — virtual monitors need Wayland.",
            "Log out and pick 'Plasma (Wayland)' on the login screen."]


def describe_primary_monitor():
    """'<name>  WxH@Hz  scale s' for the main monitor, or None."""
    try:
        main = primary_output(query_outputs())
    except Exception:
        return None
    if main is None:
        return None
    rate = f"@{main.refresh:.0f}" if main.refresh else ""
    return (f"{main.name}  {main.width}x{main.height}{rate}  "
            f"scale {main.scale:g}")


def remove_leftover_output(output_name):
    """Nothing to do: KWin removes the virtual output as soon as the
    helper's Wayland connection closes, and the helper exits with the
    daemon (stdin EOF, and SIGTERM from the kernel), so none can outlive
    it."""
    return None


def touch_setup_problem():
    """None: touches go through KWin's fake input protocol, which the
    helper's desktop entry already grants; no udev rule or root needed."""
    return None


# --------------------------------------------------------------------------- #
# backend
# --------------------------------------------------------------------------- #

class KdeBackend:
    name = "kde"
    # The virtual output dies with the helper's Wayland connection, so
    # --keep-output can't keep it: teardown() always runs.
    honors_keep_output = False

    def __init__(self, args):
        self.a = args
        self.helper = None          # KwinHelper while the output exists
        self.proc = None            # gst-launch while capturing
        # apply_live (control thread) and teardown must not interleave two
        # kscreen-doctor runs or place an output being removed.
        self._layout_lock = threading.Lock()

    @staticmethod
    def missing():
        missing = [] if shutil.which("kscreen-doctor") else ["kscreen-doctor"]
        missing += missing_pipeline_tools()
        if not kwin_helper.is_current():
            missing += [f"to build the KWin helper: {item}"
                        for item in kwin_helper.build_problem()]
        return missing

    # -- setup -------------------------------------------------------------- #

    def setup(self):
        """Build the helper if needed, create the virtual output, place it.

        Raises RuntimeError (after stopping the helper) if no output could
        be created.
        """
        if not os.environ.get("WAYLAND_DISPLAY"):
            raise RuntimeError("KDE Plasma is running on X11; virtual "
                               "monitors need a Plasma (Wayland) session")
        binary = kwin_helper.ensure_built()
        entry_written = kwin_helper.ensure_desktop_entry(binary)
        self.helper = self._create_output(binary, entry_written)
        log(f"KWin virtual output {self.helper.output_name} on pipewire "
            f"node {self.helper.node}")
        try:
            if not self._wait_until_listed():
                log(f"warning: kscreen-doctor does not list "
                    f"{self.helper.output_name} yet; it stays where KWin "
                    "put it")
                return
            with self._layout_lock:
                self._place(self.a.side, self.a.scale)
        except RuntimeError as error:
            # The monitor works where KWin put it; streaming beats failing.
            log(f"warning: could not place virtual monitor ({error})")
        except BaseException:
            self.helper.stop()
            self.helper = None
            raise

    def _create_output(self, binary, entry_written):
        """Start helpers until KWin creates an output; returns the helper.

        Retries with the next name when KWin 6.7 refuses a name it
        remembers as disabled (bug 527747), and once more after a pause
        when KWin hadn't seen a desktop entry written just now.
        """
        names = [OUTPUT_NAME] + [f"{OUTPUT_NAME}-{number}"
                                 for number in range(2, MAX_NAME_ATTEMPTS + 1)]
        permission_retry = entry_written
        index = 0
        while True:
            name = names[index]
            helper = KwinHelper(binary, name, self.a.width, self.a.height)
            try:
                created = helper.wait_for_outcome(CREATE_TIMEOUT)
            except BaseException:
                helper.stop()
                raise
            if created:
                return helper
            helper.stop()
            if helper.refused_screencast and permission_retry:
                permission_retry = False
                log("note: KWin has not picked up the helper's desktop entry "
                    "yet; trying again")
                time.sleep(PERMISSION_RETRY_DELAY)
                continue
            # KWin translates its reason into the session's language
            # ("Could not find output", "Ausgabe kann nicht gefunden
            # werden"...), so any refusal of a granted protocol moves on to
            # the next name. The only other reason KWin gives here
            # ("Unsupported compositing type") then just fails a few quick
            # times more and is reported the same way.
            if helper.failure is not None and index + 1 < len(names):
                next_output = OUTPUT_PREFIX + names[index + 1]
                log(f"note: KWin refused {helper.output_name} "
                    f"({helper.failure}; KWin 6.7 does this to a name it "
                    "remembers as disabled, KDE bug 527747); trying "
                    f"{next_output}")
                index += 1
                continue
            raise RuntimeError(self._creation_problem(helper, binary))

    @staticmethod
    def _creation_problem(helper, binary):
        """A user-facing reason the helper made no output."""
        if helper.error:
            return f"the KWin helper failed ({helper.error})"
        if helper.refused_screencast:
            return (f"KWin refused the screen-cast protocol to {binary}. "
                    "Plasma 6.7 and older only allow it for programs named "
                    f"in a desktop entry, and Tandem wrote "
                    f"{kwin_helper.desktop_entry_path()}; log out and back "
                    "in so KWin reads it")
        if helper.failure is not None:
            # Not matched on the text: KWin sends it translated.
            return (f"KWin refused the virtual monitor under every name "
                    f"Tandem tried ({helper.failure}). If KWin's reason is "
                    "'Could not find output', KWin 6.7 remembers Tandem's "
                    "monitors as disabled (KDE bug 527747): remove the "
                    "Virtual-Tandem entries from "
                    "~/.config/kwinoutputconfig.json and log out and back "
                    "in, or update Plasma")
        if helper.closed:
            return "KWin closed the virtual monitor as soon as it was made"
        if helper.exited:
            return ("the KWin helper exited unexpectedly (status "
                    f"{helper.exit_status()})")
        return (f"KWin did not create the virtual monitor within "
                f"{CREATE_TIMEOUT:.0f} s")

    def _wait_until_listed(self):
        deadline = time.monotonic() + LISTED_TIMEOUT
        while time.monotonic() < deadline:
            if any(output.name == self.helper.output_name
                   for output in query_outputs()):
                return True
            time.sleep(0.1)
        return False

    # -- placement ---------------------------------------------------------- #

    def _place(self, side, scale):
        """Place and scale our output (caller holds _layout_lock). Returns
        True if anything was sent. Raises RuntimeError when KWin rejects it."""
        own_name = self.helper.output_name
        plan = plan_layout(query_outputs(), own_name, side, scale)
        if not plan.arguments:
            return False
        dx, dy = plan.shifted
        if dx > 0 or dy > 0:
            log(f"note: moving your other monitors by {dx},{dy} so the phone "
                f"fits {side_phrase(side)} the main monitor: KWin does not "
                "allow negative positions (their modes and arrangement are "
                "kept)")
        elif dx or dy:
            log(f"note: moving your other monitors back by {dx},{dy} so the "
                "layout starts at 0,0 again")
        run_kscreen_doctor(plan.arguments)
        deadline = time.monotonic() + SETTLE_TIMEOUT
        outputs = query_outputs()
        while not layout_matches(outputs, plan, own_name):
            if time.monotonic() > deadline:
                actual = {output.name: (output.x, output.y, output.scale)
                          for output in outputs}
                log(f"warning: KWin settled on a different layout than "
                    f"asked (asked {plan.positions} at scale {plan.scale:g}, "
                    f"got {actual})")
                break
            time.sleep(0.1)
            outputs = query_outputs()
        x, y = plan.positions[own_name]
        log(f"placed {own_name} side={side} scale={plan.scale:g} at {x},{y}")
        return True

    def apply_live(self, side, scale):
        with self._layout_lock:
            if self.helper is None or not self.helper.alive:
                return False
            try:
                self._place(side, scale)
            except RuntimeError as error:
                log(f"warning: could not place virtual monitor ({error})")
                return False
        return True

    # -- capture ------------------------------------------------------------ #

    def start_capture(self):
        """Start the encoder on our PipeWire node; returns its H.264 stream.

        Safe to call again after stop_capture() (or after gst-launch exited
        on its own) within one session. Raises RuntimeError when the output
        itself is gone (disabled by the user, KWin restarted): capture can't
        come back without a new output.
        """
        helper = self.helper
        if helper is None or not helper.alive:
            reason = ("disabled in the display settings, or KWin restarted"
                      if helper is not None and helper.closed
                      else "the KWin helper stopped")
            raise RuntimeError(
                f"the KWin virtual output was closed ({reason})")
        encoder = choose_encoder(self.a, "KDE")
        self.stop_capture()                 # never two encoders on one node
        # The caps are pinned to the size we asked KWin for: KWin 6.8
        # resizes the output to match them, 6.7 only accepts its own size.
        self.proc, stream = start_encoder(helper.node, self.a, encoder)
        return stream

    def stop_capture(self):
        """Stop the encoder only (the virtual output stays). Idempotent."""
        proc, self.proc = self.proc, None
        stop_pipeline(proc)

    # -- touch -------------------------------------------------------------- #

    def create_touch_surface(self):
        helper = self.helper
        if helper is None or not helper.alive:
            raise RuntimeError("the KWin virtual output is gone")
        if not helper.fake_input_version:
            raise RuntimeError(
                "KWin refused touch input (org_kde_kwin_fake_input); on "
                "Plasma 6.7 or older log out and back in so KWin reads "
                f"{kwin_helper.desktop_entry_path()}")
        return KwinTouchscreen(helper)

    # -- teardown ----------------------------------------------------------- #

    def teardown(self):
        """Stop the helper: KWin removes the output and moves its windows to
        the remaining monitors. Idempotent, never raises."""
        with self._layout_lock:
            helper, self.helper = self.helper, None
        if helper is None:
            return
        try:
            helper.stop()
        except Exception as error:
            log(f"warning: stopping the KWin helper failed ({error})")
            return
        log(f"removed the KWin virtual output {helper.output_name}")
