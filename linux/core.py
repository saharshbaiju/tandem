"""
core.py - shared config, detection and dependency logic for Tandem.

Used by the TUI (tui.py). The streaming engine (tandemd.py) stays a standalone
CLI that the TUI launches with explicit flags built from a Config here.

Supported desktops (see backends/):
    Arch      + Hyprland          -> hyprland backend
    Ubuntu    + GNOME (Wayland)   -> gnome backend
    Fedora    + GNOME (Wayland)   -> gnome backend
"""

import glob
import importlib.util
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from urllib.parse import quote

import backends
import secure_link
import uinput_touch
from backends import gnome as gnome_backend
from backends import hyprland as hypr_backend

CONFIG_DIR = secure_link.CONFIG_DIR
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
# Shared secret a phone must prove it knows to connect at all (see
# secure_link.py); it travels only inside the QR code.
PAIRING_KEY_PATH = secure_link.PAIRING_KEY_PATH

# Quality preset -> CBR bitrate handed to the encoder.
QUALITY = {"low": "4M", "medium": "8M", "high": "12M", "ultra": "20M"}

# Android USB tethering (RNDIS/NCM) is the wired transport: it needs NO USB
# debugging. The phone becomes a USB network gadget, runs a DHCP server, and
# hands the PC an address on this subnet.
USB_TETHER_PREFIX = "192.168.42."

RES_PRESETS = [
    ("Match connected phone (USB)", None),
    ("1280x720", (1280, 720)),
    ("1920x1080", (1920, 1080)),
    ("2560x1440", (2560, 1440)),
    ("3840x2160", (3840, 2160)),
]


@dataclass
class Config:
    backend: str = "auto"          # auto / hyprland / gnome
    output: str = "EXT-1"
    side: str = "right"            # right / left / up / down
    width: int = 1920
    height: int = 1080
    fps: int = 60
    scale: float = 1.0             # desktop scale of the virtual monitor
    quality: str = "high"          # low / medium / high / ultra
    codec: str = "h264"            # the app decodes h264
    gop: int = 30                  # keyframe interval (frames)
    transport: str = "usb"         # usb / wifi (preferred address in the QR)
    port: int = 5000
    va_device: str = "/dev/dri/renderD128"
    touch: bool = True             # let paired phones control the monitor

    def bitrate(self) -> str:
        return QUALITY.get(self.quality, "12M")

    def resolved_backend(self):
        return backends.detect() if self.backend == "auto" else self.backend

    def tandemd_args(self) -> list:
        """Build the explicit CLI for tandemd.py from this config."""
        return [
            "--backend", self.resolved_backend() or "auto",
            "--output", self.output,
            "--width", str(self.width),
            "--height", str(self.height),
            "--fps", str(self.fps),
            "--scale", str(self.scale),
            "--bitrate", self.bitrate(),
            "--codec", self.codec,
            "--side", self.side,
            "--gop", str(self.gop),
            "--device", self.va_device,
            "--port", str(self.port),
            "--quality", self.quality,
            "--pairing-key-file", PAIRING_KEY_PATH,
        ] + ([] if self.touch else ["--no-touch"])


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #

def load_config() -> Config:
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH) as f:
                data = json.load(f)
            fields = Config.__dataclass_fields__
            return Config(**{k: v for k, v in data.items() if k in fields})
        except Exception:
            pass
    return Config()


def save_config(cfg: Config) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w") as f:
        json.dump(asdict(cfg), f, indent=2)


# Settings advertised to the phone. Keys must match build_banner_payload() in
# tandemd.py; the daemon also reads side/scale from here on SIGHUP and moves
# the monitor live.
BANNER_PATH = os.path.join(CONFIG_DIR, "daemon-banner.json")


def banner_payload(cfg: Config) -> dict:
    return {
        "side": cfg.side,
        "width": cfg.width,
        "height": cfg.height,
        "fps": cfg.fps,
        "scale": cfg.scale,
        "quality": cfg.quality,
        "bitrate": cfg.bitrate(),
        "codec": cfg.codec,
        "gop": cfg.gop,
        "host": socket.gethostname(),
        "backend": cfg.resolved_backend() or "",
    }


def write_banner_file(cfg: Config, path: str = BANNER_PATH) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(banner_payload(cfg), f)
    os.replace(tmp, path)          # atomic: the daemon never reads half a file


# --------------------------------------------------------------------------- #
# Distro + dependencies
# --------------------------------------------------------------------------- #

def distro() -> str:
    """'arch' / 'debian' (Ubuntu, Mint, Pop…) / 'fedora' / 'unknown'."""
    ids = ""
    try:
        with open("/etc/os-release") as f:
            for line in f:
                if line.startswith(("ID=", "ID_LIKE=")):
                    ids += " " + line.split("=", 1)[1].strip().strip('"').lower()
    except OSError:
        pass
    for key, name in (("arch", "arch"), ("fedora", "fedora"), ("rhel", "fedora"),
                      ("ubuntu", "debian"), ("debian", "debian")):
        if key in ids.split():
            return name
    return "unknown"


INSTALL_CMD = {
    "arch": ["sudo", "pacman", "-S", "--needed"],
    "debian": ["sudo", "apt", "install", "-y"],
    "fedora": ["sudo", "dnf", "install", "-y"],
}


def _bin(name):
    return lambda: shutil.which(name) is not None


def _gst(element):
    return lambda: bool(shutil.which("gst-inspect-1.0")
                        and gnome_backend.gst_inspect(element))


def _va_encode_driver():
    """(VA driver name, {distro: package}) Hyprland would encode with, or None.

    Only for Intel/AMD compositor GPUs; NVIDIA uses NVENC from its driver.
    """
    card = hypr_backend.compositor_card()
    node = hypr_backend.render_node(card) if card else None
    va = hypr_backend.va_driver_for(node) if node else None
    pkgs = {"iHD": {"arch": "intel-media-driver",
                    "debian": "intel-media-va-driver",
                    "fedora": "libva-intel-media-driver"},
            "i965": {"arch": "libva-intel-driver", "debian": "i965-va-driver",
                     "fedora": "libva-intel-driver"},
            "radeonsi": {"arch": "libva-mesa-driver",
                         "debian": "mesa-va-drivers",
                         "fedora": "mesa-va-drivers"}}.get(va)
    return (va, pkgs) if pkgs else None


def _hyprland_on_nvidia():
    card = hypr_backend.compositor_card()
    return bool(card) and hypr_backend.gpu_driver(card) == "nvidia"


def _xdph_installed():
    return any(os.path.exists(f"{d}/xdg-desktop-portal-hyprland")
               for d in ("/usr/lib", "/usr/libexec", "/usr/lib64"))


# Zero-copy capture when Hyprland renders on NVIDIA (backends/xdph.py). Without
# these it still works through wf-recorder, but Hyprland pays a copy per frame.
_NVIDIA_ZERO_COPY = [
    ("xdg-desktop-portal-hyprland", _xdph_installed,
     {"arch": "xdg-desktop-portal-hyprland",
      "debian": "xdg-desktop-portal-hyprland",
      "fedora": "xdg-desktop-portal-hyprland"}, False,
     "NVIDIA: hands frames over on the GPU (no main-screen stutter)"),
    ("python gi", lambda: importlib.util.find_spec("gi") is not None,
     {"arch": "python-gobject", "debian": "python3-gi",
      "fedora": "python3-gobject"}, False, "NVIDIA: talks to the portal"),
    ("pipewiresrc", _gst("pipewiresrc"),
     {"arch": "gst-plugin-pipewire", "debian": "gstreamer1.0-pipewire",
      "fedora": "pipewire-gstreamer"}, False, "NVIDIA: reads the portal stream"),
    ("glupload", _gst("glupload"),
     {"arch": "gst-plugins-base", "debian": "gstreamer1.0-gl",
      "fedora": "gstreamer1-plugins-base"}, False,
     "NVIDIA: imports frames into the GPU colour converter"),
    ("nvh264enc", _gst("nvh264enc"),
     {"arch": "gst-plugins-bad", "debian": "gstreamer1.0-plugins-bad",
      "fedora": "gstreamer1-plugins-bad-free"}, False,
     "NVIDIA: NVENC encoder for GPU frames"),
]


def _va_driver_present(name):
    return lambda: hypr_backend.va_installed(name)


def _any_h264_encoder():
    return bool(shutil.which("gst-inspect-1.0") and gnome_backend.pick_encoder())


# (label, check, {distro: package}, required, purpose)
DEPS = {
    "hyprland": [
        ("hyprctl", _bin("hyprctl"), {"arch": "hyprland"}, True,
         "Hyprland compositor (must be running)"),
        ("wf-recorder", _bin("wf-recorder"),
         {"arch": "wf-recorder", "debian": "wf-recorder", "fedora": "wf-recorder"},
         True, "headless capture + VA-API H.264 encode"),
        ("vainfo", _bin("vainfo"),
         {"arch": "libva-utils", "debian": "vainfo", "fedora": "libva-utils"},
         False, "VA-API capability detection"),
    ] + ([("VA-API driver", _va_driver_present(_VA[0]), _VA[1], False,
           "hardware H.264 encode on Intel/AMD (else x264 on the CPU)")]
         if (_VA := _va_encode_driver()) else [])
      + (_NVIDIA_ZERO_COPY if _hyprland_on_nvidia() else []),
    "gnome": [
        ("gst-launch-1.0", _bin("gst-launch-1.0"),
         {"arch": "gstreamer", "debian": "gstreamer1.0-tools",
          "fedora": "gstreamer1"}, True, "runs the capture/encode pipeline"),
        ("python gi", lambda: importlib.util.find_spec("gi") is not None,
         {"arch": "python-gobject", "debian": "python3-gi",
          "fedora": "python3-gobject"}, True, "talks to Mutter over D-Bus"),
        ("pipewiresrc", _gst("pipewiresrc"),
         {"arch": "gst-plugin-pipewire", "debian": "gstreamer1.0-pipewire",
          "fedora": "pipewire-gstreamer"}, True, "reads the virtual monitor"),
        ("videoconvert", _gst("videoconvert"),
         {"arch": "gst-plugins-base", "debian": "gstreamer1.0-plugins-base",
          "fedora": "gstreamer1-plugins-base"}, True, "pixel format conversion"),
        ("h264parse", _gst("h264parse"),
         {"arch": "gst-plugins-bad", "debian": "gstreamer1.0-plugins-bad",
          "fedora": "gstreamer1-plugins-bad-free"}, True,
         "H.264 framing (+ VA-API encoders)"),
        ("H.264 encoder", _any_h264_encoder,
         {"arch": "gst-plugins-ugly", "debian": "gstreamer1.0-plugins-ugly",
          "fedora": "gstreamer1-plugin-openh264"}, True,
         "x264 / openh264 (VA-API used automatically if present)"),
    ],
    "common": [
        ("openssl", _bin("openssl"),
         {"arch": "openssl", "debian": "openssl", "fedora": "openssl"}, True,
         "creates the certificate that encrypts the stream"),
        ("adb", _bin("adb"),
         {"arch": "android-tools", "debian": "adb", "fedora": "android-tools"},
         False, "phone size auto-match (needs USB debugging)"),
    ],
}


def system_python() -> bool:
    """True when running under the distro's /usr/bin/python3 — the interpreter
    the Python packages above (PyGObject) are installed into. False under
    conda / pyenv / a venv, where they stay "missing" however often they're
    installed; the `tandem` launcher (python.sh) always picks the system one."""
    try:
        return (os.path.samefile(sys.executable, "/usr/bin/python3")
                and sys.prefix == sys.base_prefix)   # not a venv
    except OSError:
        return True   # no /usr/bin/python3: nothing better to point at


def check_dependencies(backend=None) -> list:
    """[(label, package, purpose, required, present), ...] for this backend."""
    d = distro()
    rows = []
    for label, check, pkgs, req, purpose in (DEPS.get(backend, [])
                                             + DEPS["common"]):
        try:
            present = bool(check())
        except Exception:
            present = False
        rows.append((label, pkgs.get(d, next(iter(pkgs.values()))), purpose,
                     req, present))
    return rows


def missing_packages(backend=None, required_only=False) -> list:
    pkgs = []
    for _l, pkg, _p, req, present in check_dependencies(backend):
        if not present and (req or not required_only) and pkg not in pkgs:
            pkgs.append(pkg)
    return pkgs


def install_command(packages) -> list:
    base = INSTALL_CMD.get(distro())
    return base + packages if base else None


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #

def primary_monitor_desc(backend) -> str:
    """One-line description of the main display, for the hardware screen."""
    if backend == "hyprland":
        m = hypr_backend.primary_for("EXT-1")
        if m:
            return (f"{m['name']}  {m['width']}x{m['height']}"
                    f"@{m.get('refreshRate', 0):.0f}  scale {m.get('scale', 1)}")
    elif backend == "gnome":
        try:
            from gi.repository import Gio
            bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            _s, _mons, logicals, _p = gnome_backend.display_state(bus)
            for x, y, sc, _t, primary, specs, _lp in logicals:
                if primary:
                    return f"{specs[0][0]}  at {x},{y}  scale {sc:g}"
        except Exception:
            pass
    return "(none found)"


def remove_virtual_output(cfg: Config) -> None:
    """Hyprland: move windows off EXT and remove it (the TUI launches the daemon
    with --keep-output so the monitor survives restarts). GNOME's virtual
    monitor disappears by itself when the daemon stops."""
    if cfg.resolved_backend() == "hyprland":
        try:
            hypr_backend.remove_output(cfg.output)
        except Exception:
            pass


def detect_va_device() -> str:
    """Find a render node that advertises H.264 VA-API *encode*."""
    if not shutil.which("vainfo"):
        return "/dev/dri/renderD128"
    for node in sorted(glob.glob("/dev/dri/renderD*")):
        try:
            r = subprocess.run(["vainfo", "--display", "drm", "--device", node],
                               capture_output=True, text=True, timeout=5)
            if "VAEntrypointEncSlice" in r.stdout and "VAProfileH264" in r.stdout:
                return node
        except Exception:
            continue
    return "/dev/dri/renderD128"


def adb_device_present() -> bool:
    if not shutil.which("adb"):
        return False
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True,
                           text=True, timeout=5)
        return any(l.strip().endswith("device")
                   for l in r.stdout.splitlines()[1:])
    except Exception:
        return False


def adb_status() -> str:
    """'missing' (no adb), 'none' (nothing plugged in / debugging off),
    'unauthorized' (phone hasn't accepted this PC's key yet) or 'ok'."""
    if not shutil.which("adb"):
        return "missing"
    try:
        r = subprocess.run(["adb", "devices"], capture_output=True,
                           text=True, timeout=5)
    except Exception:
        return "none"
    states = [l.split()[-1] for l in r.stdout.splitlines()[1:] if l.strip()]
    if "device" in states:
        return "ok"
    return "unauthorized" if "unauthorized" in states else "none"


def _read(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def _active(unit):
    try:
        return subprocess.run(["systemctl", "is-active", "--quiet", unit],
                              timeout=5).returncode == 0
    except Exception:
        return False


def _port_in_ranges(port, items):
    """True if `port`/tcp is covered by firewalld-style '5000/tcp' or
    '1025-65535/tcp' entries."""
    for it in items:
        rng, _, proto = it.partition("/")
        if proto and proto != "tcp":
            continue
        lo, _, hi = rng.partition("-")
        try:
            if int(lo) <= port <= int(hi or lo):
                return True
        except ValueError:
            pass
    return False


def firewall_check(port: int):
    """A host firewall that may stop the phone reaching `port` over Wi-Fi /
    USB tethering, or None. Works without root (reads configs, asks
    firewalld over D-Bus). Returns {"name", "blocked": True|None (unknown),
    "fix": argv to open the port (run with sudo)}. adb (127.0.0.1) is never
    affected: firewalls accept loopback."""
    conf = _read("/etc/ufw/ufw.conf") or ""
    if re.search(r"^\s*ENABLED\s*=\s*yes", conf, re.M):
        rules = _read("/etc/ufw/user.rules")
        if rules is not None and re.search(rf"--dport {port}\b.*ACCEPT", rules):
            return None
        return {"name": "ufw", "blocked": True if rules is not None else None,
                "fix": ["sudo", "ufw", "allow", f"{port}/tcp"]}
    if shutil.which("firewall-cmd"):
        try:
            st = subprocess.run(["firewall-cmd", "--state"], capture_output=True,
                                text=True, timeout=5).stdout.strip()
        except Exception:
            st = ""
        if st == "running":
            fix = ["sudo", "sh", "-c",
                   f"firewall-cmd --add-port={port}/tcp && "
                   f"firewall-cmd --permanent --add-port={port}/tcp"]
            try:
                r = subprocess.run(["firewall-cmd", "--list-ports"],
                                   capture_output=True, text=True, timeout=5)
            except Exception:
                r = None
            if r is None or r.returncode != 0:
                return {"name": "firewalld", "blocked": None, "fix": fix}
            if _port_in_ranges(port, r.stdout.split()):
                return None     # e.g. Fedora Workstation opens 1025-65535
            return {"name": "firewalld", "blocked": True, "fix": fix}
    rule = f"tcp dport {port} accept"
    if _active("nftables"):
        conf = _read("/etc/nftables.conf") or ""
        if "policy drop" in conf and not re.search(rf"dport\s+{{?[^}}]*\b{port}\b", conf):
            return {"name": "nftables", "blocked": True,
                    "fix": ["sudo", "nft", "insert", "rule", "inet", "filter",
                            "input"] + rule.split()}
    for unit, path in (("iptables", "/etc/iptables/iptables.rules"),
                       ("netfilter-persistent", "/etc/iptables/rules.v4")):
        if _active(unit):
            conf = _read(path) or ""
            if re.search(r":INPUT (DROP|REJECT)|-P INPUT (DROP|REJECT)", conf) \
                    and f"--dport {port}" not in conf:
                return {"name": "iptables", "blocked": True,
                        "fix": ["sudo", "iptables", "-I", "INPUT", "-p", "tcp",
                                "--dport", str(port), "-j", "ACCEPT"]}
    return None


_adb_reverse_cache = {}


def adb_reverse(port: int) -> bool:
    """Forward the phone's 127.0.0.1:port to ours over the USB cable via adb.

    Works whenever the phone is plugged in with USB debugging on, even when it
    is on mobile data and can't see the PC on Wi-Fi. Re-run at most every 10 s
    (it's cheap, but the TUI asks often); True if at least one phone has it.
    """
    now = time.time()
    hit = _adb_reverse_cache.get(port)
    if hit and now - hit[0] < 10:
        return hit[1]
    ok = False
    if shutil.which("adb"):
        try:
            r = subprocess.run(["adb", "devices"], capture_output=True,
                               text=True, timeout=5)
            serials = [l.split()[0] for l in r.stdout.splitlines()[1:]
                       if l.strip().endswith("device")]
            for sn in serials:
                rr = subprocess.run(["adb", "-s", sn, "reverse",
                                     f"tcp:{port}", f"tcp:{port}"],
                                    capture_output=True, timeout=5)
                ok = ok or rr.returncode == 0
        except Exception:
            ok = False
    _adb_reverse_cache[port] = (now, ok)
    return ok


def detect_phone_size(landscape: bool = True):
    """(w, h) of a USB phone's panel, oriented for the (landscape) app."""
    if not adb_device_present():
        return None
    try:
        r = subprocess.run(["adb", "shell", "wm", "size"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"(\d+)x(\d+)", r.stdout)
        if not m:
            return None
        a, b = int(m.group(1)), int(m.group(2))
        w, h = (max(a, b), min(a, b)) if landscape else (min(a, b), max(a, b))
        return (w - w % 2, h - h % 2)   # encoders want even dimensions
    except Exception:
        return None


# Interfaces that are never a path to the phone (containers, VMs, VPN tunnels).
_VIRTUAL_IF = re.compile(r"^(docker|br-|veth|virbr|vboxnet|vmnet|tun|tap|wg|"
                         r"lxc|lxd|podman|cni|flannel|zt)")


def _ip_addrs():
    """[(ifname, ipv4), ...] for every up, non-loopback, non-virtual interface."""
    try:
        out = subprocess.run(["ip", "-j", "addr"], capture_output=True,
                             text=True, timeout=5).stdout
        links = json.loads(out)
    except Exception:
        return []
    res = []
    for link in links:
        name = link.get("ifname", "")
        if name == "lo" or _VIRTUAL_IF.match(name):
            continue
        if "UP" not in link.get("flags", []):
            continue
        for a in link.get("addr_info", []):
            if a.get("family") == "inet" and a.get("local"):
                res.append((name, a["local"]))
    return res


def lan_ip():
    try:
        r = subprocess.run(["ip", "route", "get", "1.1.1.1"],
                           capture_output=True, text=True, timeout=5)
        m = re.search(r"src (\d+\.\d+\.\d+\.\d+)", r.stdout)
        return m.group(1) if m else None
    except Exception:
        return None


# Kernel drivers of a phone's USB-tethering network gadget (Android RNDIS/NCM/
# ECM, iPhone).
TETHER_DRIVERS = ("rndis_host", "cdc_ncm", "cdc_ether", "cdc_eem", "ipheth")


def usb_tether_links():
    """[(ifname, ipv4 or None), ...] for phones tethering over USB right now.

    Matched by kernel driver, so it works whatever the interface is called
    (usb0, enp0s20f0u2, enx…) and whatever subnet the phone picked.
    """
    addrs = {}
    for name, ip in _ip_addrs():
        addrs.setdefault(name, ip)
    links = []
    for path in sorted(glob.glob("/sys/class/net/*")):
        name = os.path.basename(path)
        try:
            drv = os.path.basename(os.readlink(f"{path}/device/driver"))
        except OSError:
            continue
        if drv in TETHER_DRIVERS:
            links.append((name, addrs.get(name)))
    return links


def usb_tether_ip():
    """This PC's IPv4 on the USB-tether link, or None if no phone is tethered.

    By driver first (see usb_tether_links); failing that Android's classic
    192.168.42.0/24 subnet or a USB-gadget-looking interface name.
    """
    for _name, ip in usb_tether_links():
        if ip:
            return ip
    addrs = _ip_addrs()
    for _name, ip in addrs:
        if ip.startswith(USB_TETHER_PREFIX):
            return ip
    for name, ip in addrs:
        if name.startswith(("usb", "rndis", "ncm")) or re.search(r"u\d", name):
            return ip
    return None


TETHER_NETWORK = "/etc/systemd/network/90-tandem-usb-tether.network"


def tether_fix(ifname):
    """argv that gets `ifname` (a USB-tether link with no address) an IP from
    the phone's DHCP server, or None if we don't know how on this system.

    GNOME/KDE run NetworkManager, which does this by itself; minimal setups
    (Hyprland with iwd or systemd-networkd only) leave the link unconfigured.
    """
    if shutil.which("nmcli") and _active("NetworkManager"):
        return ["nmcli", "device", "connect", ifname]
    if _active("systemd-networkd") and shutil.which("networkctl"):
        # Persistent: every future tether link gets DHCP too.
        unit = ("[Match]\nDriver=" + " ".join(TETHER_DRIVERS) +
                "\n\n[Network]\nDHCP=ipv4\n")
        return ["sudo", "sh", "-c",
                f"printf '{unit}' > {TETHER_NETWORK} && networkctl reload "
                f"&& networkctl reconfigure {ifname}"]
    for tool, args in (("dhcpcd", ["-1"]), ("dhclient", ["-1"]),
                       ("udhcpc", ["-n", "-q", "-i"])):
        if shutil.which(tool):
            return ["sudo", "sh", "-c", f"ip link set {ifname} up && "
                    + " ".join([tool] + args + [ifname])]
    return None


def usb_state(port: int):
    """(summary, level, fix argv or None) for the wired paths, level being
    'ok' / 'action' / 'hint'. Either path works on its own: USB tethering
    (no developer options) or USB debugging (adb reverse)."""
    if adb_reverse(port):
        return "ready via USB debugging (127.0.0.1)", "ok", None
    links = usb_tether_links()
    ip = next((ip for _n, ip in links if ip), None) or usb_tether_ip()
    if ip:
        return f"USB tethering ready ({ip})", "ok", None
    if links:
        name = links[0][0]
        fix = tether_fix(name)
        return (("tethering on but no IP yet — press u", "action", fix)
                if fix else (f"tethering on but {name} has no IP (no DHCP "
                             "client)", "action", None))
    if adb_status() == "unauthorized":
        return "tap 'Allow USB debugging' on the phone", "action", None
    return "turn on USB tethering (or USB debugging)", "hint", None


def candidate_ips(cfg: Config) -> list:
    """Every address the phone might reach us on, preferred transport first."""
    tether, lan = usb_tether_ip(), lan_ip()
    adb = "127.0.0.1" if adb_reverse(cfg.port) else None
    first = ([adb, tether, lan] if cfg.transport == "usb"
             else [lan, tether, adb])
    ips = []
    for ip in first + [ip for _n, ip in _ip_addrs()]:
        if ip and ip not in ips:
            ips.append(ip)
    return ips[:5]


def connect_uri(cfg: Config) -> str:
    """What the pairing QR encodes. The app tries every host in parallel and
    keeps the first that answers, so USB and Wi-Fi both just work:

        tandem://c?h=192.168.42.129,10.0.0.5&p=5000&n=archbox&k=<key>&f=<fp>&v=2

    `k` is the pairing key: the phone proves it knows it before the computer
    sends anything (see secure_link.py). `f` is the SHA-256 fingerprint of this
    computer's TLS certificate, so the phone can tell it's talking to *this*
    computer. Creates the key and the certificate on first use.
    """
    hosts = ",".join(candidate_ips(cfg)) or "0.0.0.0"
    name = quote(socket.gethostname()[:24], safe="")
    return (f"tandem://c?h={hosts}&p={cfg.port}&n={name}"
            f"&k={pairing_key()}&f={certificate_fingerprint()}"
            f"&v={secure_link.PROTOCOL_VERSION}")


# --------------------------------------------------------------------------- #
# Pairing
# --------------------------------------------------------------------------- #

def pairing_key() -> str:
    """This computer's pairing key, created on first use (mode 0600)."""
    return secure_link.ensure_pairing_key(PAIRING_KEY_PATH)


def pairing_code() -> str:
    """The pairing key as shown for typing it in: 'xxxxx-xxxxx-xxxxx-xxxxx'."""
    return secure_link.display_key(pairing_key())


def certificate_fingerprint() -> str:
    """base64url SHA-256 of the TLS certificate (created on first use)."""
    cert, _key = secure_link.ensure_certificate()
    return secure_link.b64url(secure_link.certificate_fingerprint(cert))


def reset_pairing_key() -> str:
    """Forget every paired phone: each must scan the new QR code to connect."""
    return secure_link.reset_pairing_key(PAIRING_KEY_PATH)


# --------------------------------------------------------------------------- #
# Touch input
# --------------------------------------------------------------------------- #

UINPUT_RULE_PATH = "/etc/udev/rules.d/70-tandem-uinput.rules"
UINPUT_MODULE_PATH = "/etc/modules-load.d/tandem-uinput.conf"
# uaccess: logind grants the user at the seat (you, not other accounts or
# remote logins) access to /dev/uinput. Numbered before 73-seat-late.rules,
# which is where the tag takes effect.
UINPUT_RULE = ('KERNEL=="uinput", SUBSYSTEM=="misc", TAG+="uaccess", '
               'OPTIONS+="static_node=uinput"')


def touch_setup_check(cfg: Config):
    """What stops touch input from working on this computer, or None.

    Returns {"problem": text, "fix": argv (run with sudo) or None}. Only the
    Hyprland backend needs setup (a uinput device); GNOME injects touches
    through Mutter's RemoteDesktop API without any permissions.
    """
    if not cfg.touch or cfg.resolved_backend() != "hyprland":
        return None
    problem = uinput_touch.uinput_access_problem()
    if not problem:
        return None
    script = (f"echo {shlex.quote(UINPUT_RULE)} > {UINPUT_RULE_PATH} && "
              f"echo uinput > {UINPUT_MODULE_PATH} && modprobe uinput && "
              "udevadm control --reload && "
              "udevadm trigger --name-match=/dev/uinput && "
              "udevadm settle")
    return {"problem": problem, "fix": ["sudo", "sh", "-c", script]}
