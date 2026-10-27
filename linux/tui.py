#!/usr/bin/env python3
"""
tui.py - terminal UI for Tandem (Hyprland on Arch, GNOME on
Ubuntu / Fedora).

A self-contained curses app (no third-party deps) that:
  * checks/installs required packages for your distro + desktop on first run,
  * detects your monitors, GPU encoder and any USB phone,
  * lets you configure side / resolution / fps / quality / transport,
  * launches the streaming daemon and shows a pairing QR code the phone app
    scans — no typing IP addresses.

Run:  python3 tui.py     (or tandem)
"""

import copy
import curses
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections import deque

import about
import backends
import core
import qr

HERE = os.path.dirname(os.path.abspath(__file__))
DAEMON = os.path.join(HERE, "tandemd.py")

TITLE = " Tandem "


# --------------------------------------------------------------------------- #
# small curses helpers
# --------------------------------------------------------------------------- #

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    curses.init_pair(1, curses.COLOR_CYAN, -1)     # title / accents
    curses.init_pair(2, curses.COLOR_BLACK, curses.COLOR_CYAN)  # selection
    curses.init_pair(3, curses.COLOR_GREEN, -1)    # ok
    curses.init_pair(4, curses.COLOR_RED, -1)      # missing / stop
    curses.init_pair(5, curses.COLOR_YELLOW, -1)   # hints
    # QR code: dark glyphs on a light field, whatever the terminal theme is.
    curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_WHITE)


def addstr(win, y, x, text, attr=0):
    h, w = win.getmaxyx()
    if 0 <= y < h:
        win.addnstr(y, x, text, max(0, w - x - 1), attr)


def header(win):
    win.erase()
    addstr(win, 0, 0, TITLE, curses.color_pair(1) | curses.A_BOLD)
    h, w = win.getmaxyx()
    addstr(win, 1, 0, "─" * (w - 1), curses.color_pair(1))


def footer(win, text):
    h, w = win.getmaxyx()
    addstr(win, h - 1, 0, " " + text, curses.color_pair(5))


def show_busy(win, text):
    """Draw a one-line 'working…' screen for a blocking step."""
    header(win)
    addstr(win, 3, 2, text, curses.color_pair(5) | curses.A_BOLD)
    win.refresh()


def run_shell(win, cmd, prompt="press Enter to return"):
    """Drop out of curses, run an interactive command, come back."""
    curses.def_prog_mode()
    curses.endwin()
    print("\n$ " + " ".join(cmd) + "\n", flush=True)
    rc = 1
    try:
        rc = subprocess.run(cmd).returncode
    except Exception as e:
        print(f"error: {e}")
    input(f"\n{prompt} ")
    curses.reset_prog_mode()
    win.refresh()
    return rc


# --------------------------------------------------------------------------- #
# persistent streaming daemon controller
# --------------------------------------------------------------------------- #

# Config keys that change the capture/encode pipeline: changing any of these
# needs the daemon restarted. side/scale are applied live by the daemon itself
# (SIGHUP -> backend.apply_live); transport only changes the QR's host order.
RESTART_KEYS = ("backend", "output", "width", "height", "fps", "quality",
                "codec", "gop", "port", "touch")
LIVE_KEYS = ("side", "scale")


class Stream:
    """Owns the streaming daemon so it survives leaving the live view.

    The daemon is launched with --keep-output, so the virtual monitor (and the
    windows on it) persist across restarts triggered by realtime config edits.
    The monitor is only removed on an explicit disconnect.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.proc = None
        self.logs = deque(maxlen=8)
        self.state = {"live": 0, "ever": False}
        self.error = None
        self.touch_status = ""          # daemon's "touch input: ..." verdict
        self.stop = threading.Event()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self):
        # USB transport now rides Android USB tethering (no adb / no USB
        # debugging): the daemon binds 0.0.0.0, so it already listens on the
        # tether link — nothing to set up here. The phone connects to this PC's
        # tether IP; the pairing QR lists every reachable address.
        core.write_banner_file(self.cfg)        # settings shown on the phone
        args = self.cfg.tandemd_args()
        self.error = None
        cmd = [sys.executable, DAEMON, "--keep-output",
               "--banner-file", core.BANNER_PATH] + args
        # stdin=DEVNULL is essential: otherwise the daemon's wf-recorder child
        # inherits this terminal's stdin and swallows our keystrokes.
        self.proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, text=True, bufsize=1)
        self.stop.clear()
        threading.Thread(target=self._reader, daemon=True).start()

    def _reader(self):
        for line in self.proc.stderr:
            line = line.rstrip()
            self.logs.append(line)
            if "client connected" in line:
                self.state["live"] += 1
                self.state["ever"] = True
            elif "client disconnected" in line:
                self.state["live"] = max(0, self.state["live"] - 1)
            elif "touch input:" in line:
                self.touch_status = line.split("touch input:", 1)[1].strip()
            elif "error:" in line:
                self.error = line.split("error:", 1)[1].strip()
        self.stop.set()

    def _stop_daemon(self):
        """Terminate the daemon; the virtual output stays (--keep-output).

        The daemon needs up to ~3.5 s (encoder stop + touch device teardown),
        so the grace period is generous: SIGKILLing it early is what used to
        leave capture running on a monitor that disconnect then removed.
        """
        p = self.proc
        if p is None or p.poll() is not None:
            return
        try:
            p.terminate()
            p.wait(timeout=8)
        except subprocess.TimeoutExpired:
            p.kill()
            try:
                p.wait(timeout=2)
            except Exception:
                pass
        except Exception:
            pass

    def restart(self, cfg):
        """Relaunch the daemon with new cfg, keeping the monitor (and windows)."""
        self._stop_daemon()
        self.cfg = cfg
        self.logs.clear()
        self.state = {"live": 0, "ever": False}
        self.touch_status = ""
        self.start()

    def disconnect(self, win=None):
        """Full teardown: stop the daemon and remove the virtual monitor.

        Strictly in that order — remove_virtual_output also makes sure no
        capture is left on the monitor before Hyprland removes it. Takes a few
        seconds, so `win` (if given) shows progress instead of a frozen UI.
        """
        if win is not None:
            show_busy(win, "Disconnecting… stopping the stream")
        self._stop_daemon()
        if win is not None:
            show_busy(win, f"Disconnecting… removing {self.cfg.output}")
        core.remove_virtual_output(self.cfg)

    def push_banner(self):
        """Rewrite the banner file and tell the running daemon to re-send it,
        so connected phones immediately show the new settings."""
        core.write_banner_file(self.cfg)
        if self.running():
            try:
                self.proc.send_signal(signal.SIGHUP)
            except Exception:
                pass


def apply_config(stream, new_cfg):
    """Push edited config onto a running stream. Returns what it did."""
    old = stream.cfg
    if any(getattr(new_cfg, k) != getattr(old, k) for k in RESTART_KEYS):
        stream.restart(new_cfg)        # restart re-sends a fresh banner too
        return "restart"
    geom_changed = any(getattr(new_cfg, k) != getattr(old, k) for k in LIVE_KEYS)
    stream.cfg = new_cfg
    # The daemon re-reads the banner on SIGHUP, moves/rescales the monitor if
    # side/scale changed (no stream interruption) and pushes it to the phone.
    stream.push_banner()
    return "live" if geom_changed else "none"


# --------------------------------------------------------------------------- #
# main menu
# --------------------------------------------------------------------------- #


MENU = [
    ("Start Tandem", "start"),
    ("Configure", "configure"),
    ("Dependencies", "deps"),
    ("Detected hardware", "hardware"),
    ("Quit", "quit"),
]


def main_menu(win, cfg, stream):
    sel = 0
    running = stream is not None and stream.running()
    while True:
        header(win)
        addstr(win, 3, 2, "What do you want to do?", curses.A_BOLD)
        for i, (label, key) in enumerate(MENU):
            if key == "start" and running:
                label = "Open live view (pairing QR)"
            mark = "▶ " if i == sel else "  "
            attr = curses.color_pair(2) if i == sel else 0
            addstr(win, 5 + i, 2, f"{mark}{label}".ljust(40), attr)

        row = 6 + len(MENU)
        if running:
            live = stream.state["live"]
            if live > 0:
                addstr(win, row, 2,
                       f"● Tandem running — {live} phone"
                       f"{'s' if live > 1 else ''} connected",
                       curses.color_pair(3) | curses.A_BOLD)
            else:
                addstr(win, row, 2, "● Tandem running — waiting for phone",
                       curses.color_pair(3) | curses.A_BOLD)
        else:
            addstr(win, row, 2, "○ Tandem stopped", curses.color_pair(5))

        summ = (f"{backends.describe(cfg.resolved_backend())}   "
                f"side={cfg.side}  {cfg.width}x{cfg.height}@{cfg.fps}  "
                f"quality={cfg.quality}  port={cfg.port}")
        addstr(win, row + 1, 2, summ, curses.color_pair(5))
        footer(win, "↑/↓ move   Enter select   q quit")
        win.refresh()

        c = win.getch()
        if c in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(MENU)
        elif c in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(MENU)
        elif c in (ord("q"), 27):
            return "quit"
        elif c in (curses.KEY_ENTER, 10, 13):
            return MENU[sel][1]


# --------------------------------------------------------------------------- #
# dependencies screen
# --------------------------------------------------------------------------- #

def deps_screen(win, cfg):
    backend = cfg.resolved_backend()
    dist = core.distro()
    while True:
        header(win)
        addstr(win, 3, 2, f"Dependencies — {backends.describe(backend)} "
                          f"on {dist}", curses.A_BOLD)
        row = 5
        for (label, pkg, purpose, required, present) in core.check_dependencies(backend):
            if present:
                status, attr = "[ok]     ", curses.color_pair(3)
            else:
                status, attr = "[missing]", curses.color_pair(4)
            req = "required" if required else "optional"
            addstr(win, row, 2, status, attr)
            addstr(win, row, 13, f"{label:<15} {pkg:<28} {req:<9} {purpose}")
            row += 1

        missing = core.missing_packages(backend)
        cmd = core.install_command(missing) if missing else None
        row += 1
        if missing and cmd:
            addstr(win, row, 2, "Install with:  " + " ".join(cmd),
                   curses.color_pair(4))
            addstr(win, row + 1, 2, "Press 'i' to run it now.",
                   curses.color_pair(5))
            if dist == "fedora":
                addstr(win, row + 2, 2,
                       "Fedora: openh264 comes from the fedora-cisco-openh264 "
                       "repo (enabled by default on Workstation).",
                       curses.color_pair(5))
            if not core.system_python():
                addstr(win, row + 3, 2,
                       f"Note: running under {sys.executable}; distro packages "
                       "install into /usr/bin/python3. Start with 'tandem'.",
                       curses.color_pair(4))
        elif missing:
            addstr(win, row, 2, f"Missing: {', '.join(missing)} "
                                "(unknown distro — install them manually)",
                   curses.color_pair(4))
        else:
            addstr(win, row, 2, "All dependencies satisfied.",
                   curses.color_pair(3))
        footer(win, "i install missing   r refresh   Esc back")
        win.refresh()

        c = win.getch()
        if c == ord("i") and cmd:
            run_shell(win, cmd)
        elif c in (27, ord("q")):
            return


# --------------------------------------------------------------------------- #
# detected hardware screen
# --------------------------------------------------------------------------- #

def hardware_screen(win, cfg):
    header(win)
    addstr(win, 2, 2, "Detecting…", curses.color_pair(5))
    win.refresh()

    backend = cfg.resolved_backend()
    mon = core.primary_monitor_desc(backend)
    if backend == "hyprland":
        from backends import hyprland as hb
        enc = hb.pick_encoder("auto", core.detect_va_device())[3]
    else:
        enc = "GStreamer (best of vah264enc / x264enc / openh264enc)"
    phone = core.detect_phone_size(landscape=True)
    ip = core.lan_ip()

    header(win)
    addstr(win, 3, 2, "Detected hardware:", curses.A_BOLD)
    rows = [
        (f"Desktop         : {backends.describe(backend)}  ({core.distro()})", 0),
        (f"Primary monitor : {mon}", 0),
        (f"Encoder         : {enc}", 0),
    ]
    text, level, fix = core.usb_state(cfg.port)
    if fix:
        text = text.replace(" — press u", "") + f" — fix: {' '.join(fix)}"
    rows.append((f"USB cable       : {text}",
                 curses.color_pair(LEVEL_COLOR[level])))
    fw = core.firewall_check(cfg.port)
    rows.append((f"Firewall        : {fw['name']} "
                 f"{'blocks' if fw['blocked'] else 'may block'} port "
                 f"{cfg.port} — fix: {' '.join(fw['fix'])}"
                 if fw else "Firewall        : nothing blocking "
                 f"port {cfg.port}", curses.color_pair(4 if fw else 3)))
    if phone:
        rows.append((f"Phone panel     : {phone[0]}x{phone[1]} (landscape) — "
                     "'Match phone' available (adb)", curses.color_pair(3)))
    else:
        rows.append(("Phone panel     : auto-match needs USB debugging "
                     "(optional)", curses.color_pair(5)))
    rows.append((f"LAN IP (Wi-Fi)  : {ip or 'unknown'}", 0))
    for i, (text, attr) in enumerate(rows):
        addstr(win, 5 + i, 2, text, attr)
    addstr(win, 6 + len(rows), 2, "Press any key to return.",
           curses.color_pair(5))
    win.refresh()
    win.getch()


# --------------------------------------------------------------------------- #
# configuration editor
# --------------------------------------------------------------------------- #

def text_prompt(win, label, current):
    curses.echo()
    curses.curs_set(1)
    h, w = win.getmaxyx()
    addstr(win, h - 2, 2, f"{label}: ".ljust(w - 3), curses.color_pair(5))
    win.move(h - 2, 2 + len(label) + 2)
    try:
        raw = win.getstr(h - 2, 2 + len(label) + 2, 12).decode().strip()
    except Exception:
        raw = ""
    curses.noecho()
    curses.curs_set(0)
    return raw or str(current)


def cycle(values, current, step):
    try:
        i = values.index(current)
    except ValueError:
        i = 0
    return values[(i + step) % len(values)]


def configure_screen(win, cfg, live=False):
    """Edit a working copy of cfg.

    Returns (saved, new_cfg). On save the config is persisted to disk and the
    edited copy is returned; on cancel returns (False, None).
    """
    cfg = copy.copy(cfg)            # edit a copy so cancel truly discards
    # each entry: (label, kind, key, choices)
    rows = [
        ("Desktop backend", "choice", "backend", ["auto", "hyprland", "gnome"]),
        ("Screen side", "choice", "side", ["right", "left", "up", "down"]),
        ("Resolution", "resolution", None, None),
        ("Refresh (FPS)", "choice", "fps", [30, 60, 120]),
        ("Desktop scale", "choice", "scale", [1.0, 1.25, 1.5, 2.0]),
        ("Quality", "choice", "quality", ["low", "medium", "high", "ultra"]),
        ("Keyframe interval", "choice", "gop", [15, 30, 60]),
        ("Preferred link", "choice", "transport", ["usb", "wifi"]),
        ("Port", "port", None, None),
        ("Touch input", "choice", "touch", [True, False]),
        ("Paired phones", "forget", None, None),
    ]
    res_idx = next((i for i, (_lbl, dims) in enumerate(core.RES_PRESETS)
                    if dims == (cfg.width, cfg.height)), 0)
    sel = 0
    while True:
        header(win)
        addstr(win, 3, 2, "Configuration  (←/→ to change a value)", curses.A_BOLD)
        for i, (label, kind, key, choices) in enumerate(rows):
            if kind == "resolution":
                preset = core.RES_PRESETS[res_idx][0]
                value = f"{cfg.width}x{cfg.height}   [{preset}]"
            elif kind == "port":
                value = str(cfg.port)
            elif key == "quality":
                value = f"{cfg.quality}  ({core.QUALITY[cfg.quality]})"
            elif key == "backend":
                value = (f"auto → {cfg.resolved_backend() or 'unsupported'}"
                         if cfg.backend == "auto" else cfg.backend)
            elif key == "touch":
                value = ("on   (phones that scanned the QR can tap)"
                         if cfg.touch else "off  (view only)")
            elif kind == "forget":
                value = "Enter: forget all (new QR + code; disconnects them)"
            elif key == "transport":
                value = ("usb  (USB tethering first in the QR)"
                         if cfg.transport == "usb" else
                         "wifi (LAN address first in the QR)")
            else:
                value = str(getattr(cfg, key))
            attr = curses.color_pair(2) if i == sel else 0
            mark = "▶ " if i == sel else "  "
            addstr(win, 5 + i, 2, f"{mark}{label:<20}".ljust(24), attr)
            addstr(win, 5 + i, 28, value.ljust(40),
                   attr if i == sel else curses.color_pair(1))

        addstr(win, 6 + len(rows), 2,
               "Tip: 'Match connected phone' sizes the monitor to your phone "
               "over USB.", curses.color_pair(5))
        if live:
            addstr(win, 7 + len(rows), 2,
                   "Tandem is running — saving applies changes immediately "
                   "(side/scale instantly; others restart the stream).",
                   curses.color_pair(3))
        footer(win, "↑/↓ move   ←/→ change   Enter edit   s save & back   Esc cancel")
        win.refresh()

        c = win.getch()
        label, kind, key, choices = rows[sel]

        if c in (curses.KEY_UP, ord("k")):
            sel = (sel - 1) % len(rows)
        elif c in (curses.KEY_DOWN, ord("j")):
            sel = (sel + 1) % len(rows)
        elif c in (curses.KEY_LEFT, curses.KEY_RIGHT,
                   curses.KEY_ENTER, 10, 13):
            step = -1 if c == curses.KEY_LEFT else 1
            if kind == "choice":
                setattr(cfg, key, cycle(choices, getattr(cfg, key), step))
            elif kind == "resolution":
                res_idx = (res_idx + step) % len(core.RES_PRESETS)
                _apply_resolution(win, cfg, res_idx)
            elif kind == "port":
                raw = text_prompt(win, "Port", cfg.port)
                if raw.isdigit():
                    cfg.port = int(raw)
            elif kind == "forget" and c in (curses.KEY_ENTER, 10, 13):
                answer = text_prompt(win, "Forget all paired phones? (y/N)", "")
                if answer.strip().lower() == "y":
                    core.reset_pairing_key()
        elif c == ord("s"):
            core.save_config(cfg)
            return True, cfg
        elif c in (27, ord("q")):
            return False, None


def _apply_resolution(win, cfg, res_idx):
    label, dims = core.RES_PRESETS[res_idx]
    if dims is None:  # "match phone"
        size = core.detect_phone_size(landscape=True)
        if size:
            cfg.width, cfg.height = size
        else:
            h, w = win.getmaxyx()
            addstr(win, h - 2, 2,
                   "No USB phone detected (needs USB debugging) — "
                   "connect it and try again.", curses.color_pair(4))
            win.refresh()
            curses.napms(1200)
    else:
        cfg.width, cfg.height = dims


# --------------------------------------------------------------------------- #
# start / live status screen
# --------------------------------------------------------------------------- #

def preflight(win, cfg):
    """Refuse to start (with a clear reason) when the desktop can't work."""
    backend = cfg.resolved_backend()
    problem = None
    if backend is None:
        problem = ["This desktop isn't supported.",
                   "Run Hyprland, or GNOME on Wayland (Ubuntu / Fedora)."]
    elif backend == "gnome" and not backends.gnome.is_gnome_wayland():
        problem = ["GNOME is running on X11 — virtual monitors need Wayland.",
                   "Log out, click the gear on the login screen and pick "
                   "'Ubuntu' / 'GNOME' (not 'on Xorg')."]
    elif core.missing_packages(backend, required_only=True):
        deps_screen(win, cfg)
        if core.missing_packages(backend, required_only=True):
            return False
    if not problem:
        # The QR needs the pairing key and the certificate's fingerprint, and
        # the daemon must agree on both: create them before either starts.
        try:
            core.pairing_key()
            core.certificate_fingerprint()
        except (RuntimeError, OSError) as e:
            problem = ["Couldn't set up the encrypted connection.", str(e)]
    if problem:
        header(win)
        for i, line in enumerate(problem):
            addstr(win, 3 + i, 2, line, curses.color_pair(4 if i == 0 else 5))
        addstr(win, 4 + len(problem), 2, "Press any key to return.",
               curses.color_pair(5))
        win.refresh()
        win.getch()
        return False
    return True


def draw_qr(win, y, x, lines):
    """Draw pre-rendered half-block QR lines, black on white."""
    for i, line in enumerate(lines):
        try:
            win.addstr(y + i, x, line, curses.color_pair(6))
        except curses.error:
            pass


# Kept under ~56 columns: the live view's text column stops at the QR code.
LEVEL_COLOR = {"ok": 3, "action": 4, "hint": 5}


def draw_checks(win, y, width, fw, usb):
    """Why a phone might not reach us yet. Returns the next free row."""
    addstr(win, y, 2, "connection checks:", curses.color_pair(5))
    text, level, _fix = usb
    addstr(win, y + 1, 4, f"USB cable : {text}"[:width],
           curses.color_pair(LEVEL_COLOR[level])
           | (curses.A_BOLD if level == "action" else 0))
    if fw:
        verb = "blocks" if fw["blocked"] else "may block"
        addstr(win, y + 2, 4, f"Firewall  : {fw['name']} {verb} the phone — "
                              "press f"[:width],
               curses.color_pair(4) | curses.A_BOLD)
    else:
        addstr(win, y + 2, 4, "Firewall  : nothing blocking the port"[:width],
               curses.color_pair(3))
    addstr(win, y + 3, 4, "Wi-Fi     : same network? guest/campus Wi-Fi "
                          "isolates"[:width], curses.color_pair(5))
    return y + 4


def touch_line(cfg, stream, touch_setup):
    """(text, color pair) for the live view's Touch row."""
    if not cfg.touch:
        return "off (Configure → Touch input)", 5
    if touch_setup:
        return f"needs one-time setup ({touch_setup['problem']}) — press t", 4
    status = stream.touch_status
    if status.startswith("ready"):
        return "on — paired phones can tap", 3
    if status:
        return status, 4
    return "starting…", 5


def live_screen(win, stream):
    """Live status + pairing QR for the already-running stream.

    q / Esc  -> return to the menu, leaving the stream running.
    d        -> disconnect: stop the daemon and remove the virtual monitor.
    """
    disconnect = False
    uri, ips, pairing_code, refreshed = "", [], "", 0.0
    qr_uri, qr_lines = None, []      # QR encoding is pure Python: redo on change
    fw, usb, checked, fw_fixed = None, ("…", "hint", None), 0.0, False
    touch_setup = None
    win.nodelay(True)
    try:
        while True:
            cfg = stream.cfg                 # may change after a realtime restart
            # Addresses change when USB tethering is switched on/off; refresh
            # every couple of seconds (it shells out to `ip`).
            if time.monotonic() - refreshed > 2:
                ips = core.candidate_ips(cfg)
                uri = core.connect_uri(cfg)
                pairing_code = core.pairing_code()
                refreshed = time.monotonic()
            if time.monotonic() - checked > 5:
                fw = None if fw_fixed else core.firewall_check(cfg.port)
                usb = core.usb_state(cfg.port)
                touch_setup = core.touch_setup_check(cfg)
                checked = time.monotonic()
            live = stream.state["live"]
            exited = not stream.running()
            header(win)
            h, w = win.getmaxyx()

            if exited:
                addstr(win, 3, 2, "■ DAEMON STOPPED",
                       curses.color_pair(4) | curses.A_BOLD)
            elif live > 0:
                addstr(win, 3, 2,
                       f"● LIVE  ({live} phone{'s' if live > 1 else ''})",
                       curses.color_pair(3) | curses.A_BOLD)
            elif stream.state["ever"]:
                addstr(win, 3, 2, "○ PHONE DISCONNECTED — it reconnects by itself",
                       curses.color_pair(4) | curses.A_BOLD)
            else:
                addstr(win, 3, 2, "◌ WAITING FOR PHONE",
                       curses.color_pair(5) | curses.A_BOLD)

            addstr(win, 5, 2, f"Desktop : {backends.describe(cfg.resolved_backend())}")
            addstr(win, 6, 2, f"Monitor : {cfg.width}x{cfg.height}@{cfg.fps}  "
                              f"side={cfg.side}  scale={cfg.scale}")
            addstr(win, 7, 2, f"Stream  : h264 @ {cfg.bitrate()}  port {cfg.port}")

            # QR on the right when there's room, else below the text. Its width
            # depends on the payload (number of addresses, pairing key).
            if uri != qr_uri:
                qr_lines = qr.render_halfblocks(qr.encode(uri, "M"), quiet=2)
                qr_uri = uri
            qr_x = 64 if w > 64 + len(qr_lines[0]) else 2
            left_w = (qr_x - 4) if qr_x > 2 else w - 6   # keep text off the QR
            touch_text, touch_color = touch_line(cfg, stream, touch_setup)
            addstr(win, 8, 2, f"Touch   : {touch_text}"[:left_w],
                   curses.color_pair(touch_color))
            qr_y = 3 if qr_x > 2 else 10
            if qr_x > 2:
                addstr(win, 9, 2, "Scan the QR code with the phone app  →",
                       curses.A_BOLD)
            else:
                addstr(win, qr_y - 1, 2, "Scan with the phone app:",
                       curses.A_BOLD)
            fits = (qr_y + len(qr_lines) < h - 1
                    and qr_x + len(qr_lines[0]) < w)
            size = (len(qr_lines), len(qr_lines[0])) if fits else None
            text_y = 10 if qr_x > 2 else qr_y + (size[0] if size else 0) + 1
            if not size:
                addstr(win, text_y, 4, "(enlarge the terminal to show the QR "
                                       "code)", curses.color_pair(5))
                text_y += 1
            addstr(win, text_y, 2, "…or type an address and the code in the app:",
                   curses.color_pair(5))
            for i, ip in enumerate(ips or ["<no network — enable USB tethering "
                                           "or join Wi-Fi>"]):
                tag = "USB tethering" if ip == core.usb_tether_ip() else "network"
                addstr(win, text_y + 1 + i, 4, f"{ip}:{cfg.port}   ({tag})",
                       curses.color_pair(1) | curses.A_BOLD)
            code_y = text_y + 1 + max(1, len(ips))
            addstr(win, code_y, 4, f"pairing code  {pairing_code}"[:left_w],
                   curses.color_pair(1) | curses.A_BOLD)
            y = code_y + 2
            if not live and not exited:
                y = draw_checks(win, y, left_w, fw, usb) + 1

            if exited and stream.error:
                addstr(win, y, 2, f"error: {stream.error}", curses.color_pair(4))
                y += 1
            addstr(win, y + 1, 2, "daemon log:", curses.color_pair(5))
            for i, line in enumerate(list(stream.logs)[-max(0, h - y - 4):]):
                addstr(win, y + 2 + i, 4, line[:left_w], curses.color_pair(5))
            if size:
                draw_qr(win, qr_y, qr_x, qr_lines)   # last, so nothing overlaps it

            footer(win, "q/Esc back (keeps streaming)   d disconnect & remove "
                        "monitor" + ("   f open firewall" if fw and not live
                                     else "")
                        + ("   u fix USB link" if usb[2] and not live else "")
                        + ("   t set up touch" if touch_setup else "")
                        + ("   — daemon stopped, any key" if exited else ""))
            win.refresh()

            c = win.getch()
            if c in (ord("d"), ord("D")):
                disconnect = True
                break
            if c in (ord("f"), ord("F")) and fw:
                if run_shell(win, fw["fix"]) == 0:
                    fw_fixed, fw = True, None     # nft/iptables: not re-detectable
                continue
            if c in (ord("t"), ord("T")) and touch_setup:
                if run_shell(win, touch_setup["fix"]) == 0:
                    stream.restart(cfg)           # daemon re-creates the device
                checked = 0.0
                continue
            if c in (ord("u"), ord("U")) and usb[2]:
                run_shell(win, usb[2], "done — rescan the QR on the phone; "
                                       "press Enter")
                checked = refreshed = 0.0         # new address -> new QR
                continue
            if c in (ord("q"), 27) or (exited and c != -1):
                disconnect = disconnect or exited
                break
            curses.napms(150)
    finally:
        win.nodelay(False)
        if disconnect:
            stream.disconnect(win)


# --------------------------------------------------------------------------- #
# entry
# --------------------------------------------------------------------------- #

def app(stdscr):
    curses.curs_set(0)
    curses.set_escdelay(25)        # snappy, unambiguous ESC vs. arrow keys
    stdscr.keypad(True)            # translate arrow/function keys to KEY_*
    init_colors()
    cfg = core.load_config()
    stream = None

    # first run: go straight to dependencies if anything required is missing
    if core.missing_packages(cfg.resolved_backend(), required_only=True):
        deps_screen(stdscr, cfg)

    while True:
        choice = main_menu(stdscr, cfg, stream)
        if choice == "quit":
            if stream and stream.running():
                stream.disconnect(stdscr)
            break
        elif choice == "configure":
            running = stream is not None and stream.running()
            saved, new_cfg = configure_screen(stdscr, cfg, live=running)
            if saved:
                cfg = new_cfg
                if running:
                    apply_config(stream, cfg)   # realtime: live or restart
        elif choice == "deps":
            deps_screen(stdscr, cfg)
        elif choice == "hardware":
            hardware_screen(stdscr, cfg)
        elif choice == "start":
            if stream is None or not stream.running():
                if not preflight(stdscr, cfg):
                    continue
                stream = Stream(cfg)
                stream.start()
            live_screen(stdscr, stream)
            if not stream.running():        # disconnected / died
                stream = None


# Where distros keep the terminfo database. An interpreter that bundles its own
# ncurses (conda/miniconda) only looks in its own prefix and can't find it.
SYSTEM_TERMINFO_DIRS = ("/usr/share/terminfo", "/usr/lib/terminfo",
                        "/etc/terminfo", "/lib/terminfo")


def setup_terminal():
    """Make sure curses can load a terminfo entry for this terminal.

    `tandem` runs the system Python, whose curses finds the system database.
    Run directly under conda (`python3 tui.py`), setupterm fails with "could
    not find terminfo database": point it at the system directories, and if
    the terminal's entry (e.g. xterm-kitty) still isn't there, fall back to
    xterm-256color, which every terminal emulator understands well enough.
    """
    def loads():
        try:
            curses.setupterm(os.environ.get("TERM", "unknown"),
                             sys.stdout.fileno())
            return True
        except curses.error:
            return False

    if loads():
        return
    dirs = [d for d in SYSTEM_TERMINFO_DIRS if os.path.isdir(d)]
    if os.environ.get("TERMINFO_DIRS"):
        dirs.insert(0, os.environ["TERMINFO_DIRS"])
    os.environ["TERMINFO_DIRS"] = ":".join(dirs)
    if loads():
        return
    os.environ["TERM"] = "xterm-256color"
    loads()   # if even this fails, curses.wrapper reports the real error


def main():
    if sys.argv[1:] in (["--version"], ["-V"]):
        print(f"{about.CREDIT}\n{about.LICENSE}")   # the credit NOTICE asks forks to keep
        return
    setup_terminal()
    try:
        curses.wrapper(app)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
