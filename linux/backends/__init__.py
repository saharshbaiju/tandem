"""
Compositor backends: each one creates a virtual monitor and turns it into a raw
Annex-B H.264 byte stream that tandemd parses and broadcasts.

    hyprland : Hyprland headless output + wf-recorder (VA-API), or the
               portal + GStreamer NVENC when Hyprland renders on NVIDIA [Arch]
    gnome    : Mutter ScreenCast virtual monitor + GStreamer/PipeWire [Ubuntu, Fedora]

Backend interface (duck-typed):
    missing()            -> list of missing tools (static)
    setup()              -> create the virtual monitor / capture session
    start_capture()      -> readable binary stream of Annex-B H.264
    apply_live(side, scale) -> re-place / re-scale without restarting
    stop_capture(), teardown()
"""

import os

from . import gnome, hyprland

NAMES = ("hyprland", "gnome")


def detect():
    """Best backend for the running session, or None if unsupported."""
    if os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        return "hyprland"
    if gnome.is_gnome_wayland():
        return "gnome"
    desk = os.environ.get("XDG_CURRENT_DESKTOP", "").lower()
    if "hyprland" in desk:
        return "hyprland"
    if "gnome" in desk:
        return "gnome"          # X11 GNOME: setup() will explain the failure
    return None


def create(name, args):
    if name in (None, "auto"):
        name = detect()
    if name == "hyprland":
        return hyprland.HyprlandBackend(args)
    if name == "gnome":
        return gnome.GnomeBackend(args)
    raise SystemExit("unsupported desktop: run Hyprland, or GNOME on Wayland "
                     "(Ubuntu / Fedora). Use --backend to force one.")


def describe(name):
    return {"hyprland": "Hyprland (headless output + wf-recorder / portal)",
            "gnome": "GNOME / Mutter (virtual monitor + PipeWire)"}.get(
                name, "unsupported desktop")
