"""
Compositor backends: each one creates a virtual monitor and turns it into a raw
Annex-B H.264 byte stream that tandemd parses and broadcasts.

    hyprland : Hyprland headless output + wf-recorder (VA-API), or the
               portal + GStreamer NVENC when Hyprland renders on NVIDIA [Arch]
    gnome    : Mutter ScreenCast virtual monitor + GStreamer/PipeWire [Ubuntu, Fedora]
    kde      : KWin virtual output (tandem-kwin-helper, built by kwin_helper.py)
               + kscreen-doctor placement + GStreamer/PipeWire [any Plasma 6,
               beta]

Shared helpers (not backends): gstreamer.py (PipeWire -> H.264 through
gst-launch), layout.py (where the phone's monitor goes), xdph.py (the
Hyprland portal path).

Backend class interface (duck-typed), created as Class(args) with tandemd's
argparse Namespace; the constructor must not touch the compositor:
    name                 -> class attribute, its key in _REGISTRY
    honors_keep_output   -> class attribute: True if the virtual monitor can
                            outlive the daemon, so --keep-output skips
                            teardown() (Hyprland). False when it dies with
                            the daemon's connection anyway (GNOME, KDE).
    missing()            -> list of missing tools (static)
    setup()              -> create the virtual monitor / capture session.
                            Raises on failure, after cleaning up whatever it
                            created: teardown() is not called then.
    start_capture()      -> readable binary stream of Annex-B H.264
    apply_live(side, scale) -> re-place / re-scale without restarting
    create_touch_surface() -> touch surface (slot_count, description,
                            down/move/up/release_all/close), or raises a
                            RuntimeError whose text is shown to the user
    stop_capture(), teardown()

start_capture() / stop_capture() may be called again within one session
(after the encoder exits, the daemon restarts capture without tearing down
the virtual output); stop_capture() is idempotent.

Optional:
    request_keyframe()   -> True if an IDR will follow soon. Look it up with
                            getattr(backend, "request_keyframe", None): the
                            gst-launch based paths (gnome, xdph) can't take
                            events from outside, so they don't have it.

Module-level hooks every backend module defines. The TUI and the installer
call them (through module(name)) without a backend instance, often while no
daemon runs, so each must be cheap, side-effect free unless stated, and never
raise:
    session_confidence()        -> int: how sure it is that this session is
                                   its desktop. 3: the compositor's own IPC
                                   variable is set (Hyprland's
                                   HYPRLAND_INSTANCE_SIGNATURE); 2:
                                   XDG_CURRENT_DESKTOP names it in a
                                   session it can work in; 1: only
                                   the name matches (e.g. GNOME on X11, where
                                   setup() explains the failure); 0: not it.
    session_problem()           -> list of lines saying why this session
                                   can't work (GNOME / KDE on X11), or None
    describe_primary_monitor()  -> one line about the main monitor, or None
    remove_leftover_output(output_name) -> remove a virtual output a daemon
                                   started with --keep-output left behind
                                   (a no-op where it dies with the daemon)
    touch_setup_problem()       -> {"problem": text, "fix": argv or None}
                                   for one-time touch setup the user can
                                   run (TUI key t, install.sh), or None.
                                   Callers must not assume the fix uses
                                   sudo; None as fix means nothing can be
                                   run, only explained

Adding a backend:
    1. Write backends/<module>.py with the class and the hooks above. Keep
       its top-level imports side-effect free and tolerant of missing
       optional modules (see gnome.py's gi import): every registered module
       is imported whenever `backends` is.
    2. Add one _REGISTRY entry below. Its position is the display order (the
       TUI's choice list, tandemd's --backend choices) and breaks ties in
       detect().
    3. Add its package rows to core.DEPS under the same name.
"""

import importlib
from typing import NamedTuple


# Where users report problems; beta_notice() points them here.
ISSUES_URL = "https://github.com/saharshbaiju/tandem/issues"


class Entry(NamedTuple):
    module: str         # module name inside this package
    class_name: str     # the backend class in that module
    description: str    # one line for the daemon log and the TUI
    desktop: str        # how messages to the user name the desktop
    beta_name: str = ""  # set while a backend is in beta: the desktop's
                         # short name for beta_notice(). Its description
                         # and desktop then say "beta" too.


_REGISTRY = {
    "hyprland": Entry("hyprland", "HyprlandBackend",
                      "Hyprland (headless output + wf-recorder / portal)",
                      "Hyprland"),
    "gnome": Entry("gnome", "GnomeBackend",
                   "GNOME / Mutter (virtual monitor + PipeWire)",
                   "GNOME on Wayland (Ubuntu / Fedora)"),
    "kde": Entry("kde", "KdeBackend",
                 "KDE Plasma / KWin (virtual output + PipeWire, beta)",
                 "KDE Plasma on Wayland (beta)",
                 beta_name="KDE Plasma"),
}

NAMES = tuple(_REGISTRY)

# Imported up front: tandemd and the TUI use them all, and a bug in one
# should show at startup, not on the first click that reaches it. Importing
# a submodule also makes it an attribute of this package (backends.gnome).
_MODULES = {name: importlib.import_module(f".{entry.module}", __name__)
            for name, entry in _REGISTRY.items()}


def module(name):
    """The module of backend `name` (for its hooks), or None if unknown."""
    return _MODULES.get(name)


def _session_confidence(name):
    try:
        return int(_MODULES[name].session_confidence())
    except Exception:
        return 0


def detect():
    """Best backend for the running session, or None if unsupported.

    The backend most sure of itself wins (see session_confidence() above),
    so a compositor's own IPC variable beats the desktop name; ties go to
    the earlier entry in NAMES.
    """
    best_name, best_confidence = None, 0
    for name in NAMES:
        confidence = _session_confidence(name)
        if confidence > best_confidence:
            best_name, best_confidence = name, confidence
    return best_name


def supported_desktops():
    """Every desktop a backend exists for, as one phrase for "this desktop
    isn't supported" messages: the entries' `desktop` names joined with
    commas and a final "or" ("Hyprland, GNOME on Wayland ..., or KDE
    Plasma ...")."""
    desktops = [entry.desktop for entry in _REGISTRY.values()]
    if len(desktops) == 1:
        return desktops[0]
    return ", ".join(desktops[:-1]) + ", or " + desktops[-1]


def create(name, args):
    """A backend instance for `name` ("auto" or None: detect())."""
    if name in (None, "auto"):
        name = detect()
    entry = _REGISTRY.get(name)
    if entry is None:
        raise SystemExit(f"unsupported desktop: run {supported_desktops()}. "
                         "Use --backend to force one.")
    return getattr(_MODULES[name], entry.class_name)(args)


def describe(name):
    entry = _REGISTRY.get(name)
    return entry.description if entry else "unsupported desktop"


def beta_notice(name):
    """One line telling the user backend `name` is in beta and where to
    report problems, or None for a stable (or unknown) backend."""
    entry = _REGISTRY.get(name)
    if entry is None or not entry.beta_name:
        return None
    return (f"{entry.beta_name} support is in beta; please report problems "
            f"at {ISSUES_URL}")
