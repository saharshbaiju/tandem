"""
keyboard.py - typing from the phone: keysym -> key presses on the computer.

The phone sends X keysyms (KEYS frames, see phone_input.py): what the user
meant ("a", "A", "é", Return, Control_L), not which physical key to press,
because the phone has no idea which keyboard layout the computer uses.

How a keysym becomes a key press depends on the desktop:

  * GNOME: Mutter takes keysyms directly (RemoteDesktop NotifyKeyboardKeysym)
    and finds the key in the user's layout itself.
  * KDE Plasma 6.2 and later: KWin takes keysyms directly too
    (org_kde_kwin_fake_input v6 keyboard_keysym).
  * Hyprland, and older KWin: only key *codes* can be injected (a uinput
    keyboard, fake_input v4 keyboard_key), so this module looks the keysym
    up in the user's layout with libxkbcommon: which key, and which
    modifiers (Shift, AltGr) give it. A character the layout can't type is
    skipped (logged once).

libxkbcommon is loaded through ctypes; every Wayland desktop has it.
"""

import ctypes
import ctypes.util
import fcntl
import os
import struct
import sys
import threading
import time

import uinput_touch as ui

# --- X keysyms the lookup treats specially ---------------------------------- #

UNICODE_KEYSYM_BASE = 0x01000000     # keysym = this | code point (X11 rule)
MODIFIER_KEYSYMS = {
    0xFFE1, 0xFFE2,          # Shift_L, Shift_R
    0xFFE3, 0xFFE4,          # Control_L, Control_R
    0xFFE9, 0xFFEA,          # Alt_L, Alt_R
    0xFFEB, 0xFFEC,          # Super_L, Super_R
    0xFE03,                  # ISO_Level3_Shift (AltGr)
}

# evdev key codes (linux/input-event-codes.h) used as modifiers
KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_RIGHTALT = 29, 42, 100
XKB_LOG_LEVEL_CRITICAL = 10           # our own error says what went wrong
EVDEV_OFFSET = 8                     # xkb keycode = evdev code + 8
MAX_EVDEV_KEY = 248                  # KEY_MICMUTE; the rest are buttons


def log(*a):
    try:
        print("[tandemd]", *a, file=sys.stderr, flush=True)
    except (OSError, ValueError):
        pass


# --------------------------------------------------------------------------- #
# libxkbcommon
# --------------------------------------------------------------------------- #

class _RuleNames(ctypes.Structure):
    _fields_ = [(name, ctypes.c_char_p)
                for name in ("rules", "model", "layout", "variant", "options")]


def _load_xkbcommon():
    name = ctypes.util.find_library("xkbcommon") or "libxkbcommon.so.0"
    lib = ctypes.CDLL(name)
    vp, u32, i32 = ctypes.c_void_p, ctypes.c_uint32, ctypes.c_int
    lib.xkb_context_new.restype = vp
    lib.xkb_context_new.argtypes = [i32]
    lib.xkb_context_unref.argtypes = [vp]
    lib.xkb_context_set_log_level.argtypes = [vp, i32]
    lib.xkb_keymap_new_from_names.restype = vp
    lib.xkb_keymap_new_from_names.argtypes = [vp, ctypes.POINTER(_RuleNames), i32]
    lib.xkb_keymap_unref.argtypes = [vp]
    lib.xkb_keymap_min_keycode.restype = u32
    lib.xkb_keymap_min_keycode.argtypes = [vp]
    lib.xkb_keymap_max_keycode.restype = u32
    lib.xkb_keymap_max_keycode.argtypes = [vp]
    lib.xkb_keymap_num_levels_for_key.restype = u32
    lib.xkb_keymap_num_levels_for_key.argtypes = [vp, u32, u32]
    lib.xkb_keymap_key_get_syms_by_level.restype = i32
    lib.xkb_keymap_key_get_syms_by_level.argtypes = [
        vp, u32, u32, u32, ctypes.POINTER(ctypes.POINTER(u32))]
    lib.xkb_keymap_key_get_mods_for_level.restype = ctypes.c_size_t
    lib.xkb_keymap_key_get_mods_for_level.argtypes = [
        vp, u32, u32, u32, ctypes.POINTER(u32), ctypes.c_size_t]
    lib.xkb_keymap_mod_get_index.restype = u32
    lib.xkb_keymap_mod_get_index.argtypes = [vp, ctypes.c_char_p]
    lib.xkb_keysym_to_utf32.restype = u32
    lib.xkb_keysym_to_utf32.argtypes = [u32]
    return lib


_xkb = None
_xkb_lock = threading.Lock()


def xkbcommon():
    """The loaded library, or raises OSError (it isn't installed)."""
    global _xkb
    with _xkb_lock:
        if _xkb is None:
            _xkb = _load_xkbcommon()
        return _xkb


class Keymap:
    """keysym -> (evdev key code, modifier key codes to hold) in one layout.

    Built once from RMLVO names (the same ones the compositor uses), so the
    computer types exactly what the phone's keyboard showed. Only the first
    layout of a multi-layout setting is searched: a uinput keyboard types in
    whatever layout is active, and the first one is the usual one.
    """

    def __init__(self, layout="", variant="", options="", model=""):
        lib = xkbcommon()
        names = _RuleNames(None, _bytes(model), _bytes(layout),
                           _bytes(variant), _bytes(options))
        context = lib.xkb_context_new(0)
        if not context:
            raise RuntimeError("libxkbcommon could not start")
        lib.xkb_context_set_log_level(context, XKB_LOG_LEVEL_CRITICAL)
        try:
            keymap = lib.xkb_keymap_new_from_names(context, ctypes.byref(names), 0)
            if not keymap:
                raise RuntimeError(f"no keyboard layout named {layout!r}")
            try:
                self._by_keysym, self._by_char = _index(lib, keymap)
            finally:
                lib.xkb_keymap_unref(keymap)
        finally:
            lib.xkb_context_unref(context)
        self.layout = layout or "us"

    def lookup(self, keysym):
        """(code, [modifier codes]) that types `keysym`, or None."""
        found = self._by_keysym.get(keysym)
        if found:
            return found
        char = (keysym & 0xFFFFFF if keysym & UNICODE_KEYSYM_BASE
                else _keysym_char(keysym))
        return self._by_char.get(char) if char else None


def _bytes(text):
    return text.encode() if text else None


def _keysym_char(keysym):
    try:
        return xkbcommon().xkb_keysym_to_utf32(keysym) or 0
    except OSError:
        return 0


def _index(lib, keymap):
    """Every keysym of layout 0 -> the simplest way to type it."""
    modifier_keys = {}
    for mod_name, code in ((b"Shift", KEY_LEFTSHIFT), (b"Control", KEY_LEFTCTRL),
                           (b"Mod5", KEY_RIGHTALT)):
        index = lib.xkb_keymap_mod_get_index(keymap, mod_name)
        if index != 0xFFFFFFFF:
            modifier_keys[1 << index] = code

    by_keysym, by_char = {}, {}
    syms = ctypes.POINTER(ctypes.c_uint32)()
    masks = (ctypes.c_uint32 * 8)()
    for keycode in range(lib.xkb_keymap_min_keycode(keymap),
                         lib.xkb_keymap_max_keycode(keymap) + 1):
        code = keycode - EVDEV_OFFSET
        if not 0 < code <= MAX_EVDEV_KEY:
            continue
        for level in range(lib.xkb_keymap_num_levels_for_key(keymap, keycode, 0)):
            count = lib.xkb_keymap_key_get_syms_by_level(keymap, keycode, 0,
                                                         level, ctypes.byref(syms))
            if count != 1:
                continue
            modifiers = _modifiers_for(lib, keymap, keycode, level, masks,
                                       modifier_keys)
            if modifiers is None:
                continue
            entry = (code, modifiers)
            keysym = syms[0]
            # Fewest modifiers win: "1" from the number row, not the keypad
            # with NumLock; the first key found wins a tie.
            for table, key in ((by_keysym, keysym),
                               (by_char, lib.xkb_keysym_to_utf32(keysym))):
                if key and (key not in table
                            or len(table[key][1]) > len(modifiers)):
                    table[key] = entry
    return by_keysym, by_char


def _modifiers_for(lib, keymap, keycode, level, masks, modifier_keys):
    """Modifier key codes that select `level`, or None if it needs one we
    can't press (NumLock, a locked group)."""
    count = lib.xkb_keymap_key_get_mods_for_level(keymap, keycode, 0, level,
                                                  masks, len(masks))
    best = None
    for i in range(count):
        mask, codes = masks[i], []
        for bit, code in modifier_keys.items():
            if mask & bit:
                codes.append(code)
                mask &= ~bit
        if mask == 0 and (best is None or len(codes) < len(best)):
            best = codes
    return best


# --------------------------------------------------------------------------- #
# Typing through key codes
# --------------------------------------------------------------------------- #

class KeyCodeTyper:
    """Turns keysym presses into key-code presses through `press(code, down)`.

    A keysym that needs a modifier the user isn't holding (Shift for "A",
    AltGr for "@" on a German layout) gets it pressed around it; modifier
    keysyms the phone sends itself (Ctrl for Ctrl+C) are pressed as keys.
    """

    def __init__(self, keymap, press):
        self._keymap = keymap
        self._press = press
        self._down = {}               # keysym -> (code, added modifier codes)
        self._unknown = set()

    def key(self, keysym, pressed):
        if not pressed:
            held = self._down.pop(keysym, None)
            if held:
                code, added = held
                self._press(code, False)
                for modifier in reversed(added):
                    self._press(modifier, False)
            return
        if keysym in self._down:
            return                    # already down (a repeated press)
        found = self._keymap.lookup(keysym)
        if not found:
            if keysym not in self._unknown and len(self._unknown) < 32:
                self._unknown.add(keysym)
                log(f"keyboard: {self._keymap.layout} layout has no key for "
                    f"keysym {keysym:#x}; skipped")
            return
        code, modifiers = found
        held_codes = {c for c, _ in self._down.values()}
        added = [] if keysym in MODIFIER_KEYSYMS else [
            m for m in modifiers if m not in held_codes]
        for modifier in added:
            self._press(modifier, True)
        self._press(code, True)
        self._down[keysym] = (code, added)

    def release_all(self):
        # Ordinary keys first, so a held Ctrl is lifted after its letter.
        for keysym in sorted(self._down, key=lambda k: k in MODIFIER_KEYSYMS):
            self.key(keysym, False)


# --------------------------------------------------------------------------- #
# uinput keyboard (Hyprland)
# --------------------------------------------------------------------------- #

KEYBOARD_DEVICE_NAME = "tandem-keyboard"


class VirtualKeyboard:
    """A kernel keyboard through /dev/uinput that types keysyms in `keymap`.

    The compositor gives it the same layout as the user's real keyboard
    (the layout `keymap` was built from), so the codes come out as the
    intended characters. Key repeat is the compositor's job, as for any
    keyboard; the phone sends one press and one release.
    """

    def __init__(self, keymap, name=KEYBOARD_DEVICE_NAME):
        self.description = f"uinput keyboard '{name}' ({keymap.layout} layout)"
        self._fd = os.open(ui.UINPUT_PATH, os.O_WRONLY | os.O_NONBLOCK)
        try:
            fcntl.ioctl(self._fd, ui.UI_SET_EVBIT, ui.EV_KEY)
            for code in range(1, MAX_EVDEV_KEY + 1):
                fcntl.ioctl(self._fd, ui.UI_SET_KEYBIT, code)
            # struct uinput_setup, as in uinput_touch (its own product id)
            fcntl.ioctl(self._fd, ui.UI_DEV_SETUP,
                        struct.pack("HHHH80sI", ui.BUS_VIRTUAL, 0x1209, 0xA5D2,
                                    1, name.encode()[:79], 0))
            fcntl.ioctl(self._fd, ui.UI_DEV_CREATE)
            time.sleep(0.2)       # let the compositor pick the device up
        except OSError:
            os.close(self._fd)
            raise
        self._typer = KeyCodeTyper(keymap, self._press)
        self._lock = threading.Lock()

    def key(self, keysym, pressed):
        with self._lock:
            if self._fd is not None:
                self._typer.key(keysym, pressed)

    def release_all(self):
        with self._lock:
            if self._fd is not None:
                self._typer.release_all()

    def close(self):
        with self._lock:
            if self._fd is None:
                return
            try:
                self._typer.release_all()
                fcntl.ioctl(self._fd, ui.UI_DEV_DESTROY)
            except OSError:
                pass
            os.close(self._fd)
            self._fd = None

    def _press(self, code, down):
        now = time.time()
        sec, usec = int(now), int((now % 1) * 1_000_000)
        os.write(self._fd,
                 ui.INPUT_EVENT.pack(sec, usec, ui.EV_KEY, code, int(down))
                 + ui.INPUT_EVENT.pack(sec, usec, ui.EV_SYN, ui.SYN_REPORT, 0))
