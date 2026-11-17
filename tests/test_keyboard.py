"""Tests for typing from the phone: the KEYS frame and key routing
(phone_input.py), keysym lookup and modifier handling (keyboard.py), and the
KDE / Hyprland layout readers.

The xkbcommon lookups use the real libxkbcommon and are skipped where it
isn't installed; nothing here creates an input device.
"""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import json
import os
import tempfile
import threading
import unittest
from unittest import mock

import keyboard
import phone_input
from backends import hyprland, kde

KEY_A, KEY_C = 30, 46
XK_a, XK_A, XK_c, XK_at = 0x61, 0x41, 0x63, 0x40
XK_Control_L, XK_Return = 0xFFE3, 0xFF0D


def have_xkbcommon():
    try:
        keyboard.xkbcommon()
        return True
    except OSError:
        return False


class FakeKeymap:
    """The few keys these tests type, on a US-like layout."""
    layout = "test"
    TABLE = {XK_a: (KEY_A, []), XK_A: (KEY_A, [keyboard.KEY_LEFTSHIFT]),
             XK_c: (KEY_C, []), XK_Control_L: (keyboard.KEY_LEFTCTRL, []),
             XK_at: (16, [keyboard.KEY_RIGHTALT])}

    def lookup(self, keysym):
        return self.TABLE.get(keysym)


class KeyCodeTyperTests(unittest.TestCase):
    def setUp(self):
        self.presses = []
        self.typer = keyboard.KeyCodeTyper(
            FakeKeymap(), lambda code, down: self.presses.append((code, down)))

    def test_plain_key(self):
        self.typer.key(XK_a, True)
        self.typer.key(XK_a, False)
        self.assertEqual(self.presses, [(KEY_A, True), (KEY_A, False)])

    def test_shift_is_added_around_a_capital(self):
        self.typer.key(XK_A, True)
        self.typer.key(XK_A, False)
        shift = keyboard.KEY_LEFTSHIFT
        self.assertEqual(self.presses, [(shift, True), (KEY_A, True),
                                        (KEY_A, False), (shift, False)])

    def test_ctrl_shortcut_from_the_phone(self):
        for keysym, down in ((XK_Control_L, True), (XK_c, True),
                             (XK_c, False), (XK_Control_L, False)):
            self.typer.key(keysym, down)
        ctrl = keyboard.KEY_LEFTCTRL
        self.assertEqual(self.presses, [(ctrl, True), (KEY_C, True),
                                        (KEY_C, False), (ctrl, False)])

    def test_unknown_keysym_is_skipped(self):
        with mock.patch.object(keyboard, "log") as log:
            self.typer.key(0x1F600, True)
            self.typer.key(0x1F600, True)
            self.typer.key(0x1F600, False)
        self.assertEqual(self.presses, [])
        self.assertEqual(log.call_count, 1)      # said once, not per press

    def test_repeated_press_and_stray_release(self):
        self.typer.key(XK_a, True)
        self.typer.key(XK_a, True)
        self.typer.key(XK_c, False)
        self.assertEqual(self.presses, [(KEY_A, True)])

    def test_release_all_lets_go_of_letters_before_ctrl(self):
        self.typer.key(XK_Control_L, True)
        self.typer.key(XK_c, True)
        self.presses.clear()
        self.typer.release_all()
        self.assertEqual(self.presses, [(KEY_C, False),
                                        (keyboard.KEY_LEFTCTRL, False)])


@unittest.skipUnless(have_xkbcommon(), "libxkbcommon is not installed")
class KeymapTests(unittest.TestCase):
    def test_us_layout(self):
        keymap = keyboard.Keymap("us")
        self.assertEqual(keymap.lookup(XK_a), (KEY_A, []))
        self.assertEqual(keymap.lookup(XK_A), (KEY_A, [keyboard.KEY_LEFTSHIFT]))
        self.assertEqual(keymap.lookup(XK_Return), (28, []))
        self.assertIsNone(keymap.lookup(0x1F600))           # an emoji

    def test_altgr_and_unicode_keysyms(self):
        keymap = keyboard.Keymap("de")
        self.assertEqual(keymap.lookup(XK_at), (16, [keyboard.KEY_RIGHTALT]))
        euro = keymap.lookup(0x20AC)                         # EuroSign
        self.assertIsNotNone(euro)
        # The phone sends "€" as a Unicode keysym; it finds the same key.
        self.assertEqual(keymap.lookup(keyboard.UNICODE_KEYSYM_BASE | 0x20AC),
                         euro)
        self.assertEqual(keymap.lookup(0xE4), (40, []))      # ä

    def test_unknown_layout(self):
        with self.assertRaises(RuntimeError):
            keyboard.Keymap("no-such-layout-xyz")


class RecordingKeyboard:
    def __init__(self):
        self.calls = []

    def key(self, keysym, pressed):
        self.calls.append((keysym, pressed))


class Client:
    alive = True
    addr = ("192.0.2.1", 1)

    def __init__(self):
        self.sent = []

    def send_message(self, frame_type, body):
        self.sent.append((frame_type, body))


class KeyRouterTests(unittest.TestCase):
    def test_disconnect_releases_held_keys_letters_first(self):
        board = RecordingKeyboard()
        router = phone_input.KeyRouter(board)
        phone, other = object(), object()
        router.handle(phone, XK_Control_L, True)
        router.handle(phone, XK_c, True)
        router.handle(other, XK_a, True)
        board.calls.clear()
        router.client_gone(phone)
        self.assertEqual(board.calls, [(XK_c, False), (XK_Control_L, False)])

    def test_release_without_press_is_dropped(self):
        board = RecordingKeyboard()
        phone_input.KeyRouter(board).handle(object(), XK_a, False)
        self.assertEqual(board.calls, [])


class PhoneInputKeyTests(unittest.TestCase):
    def make(self, touch=False):
        board = RecordingKeyboard()
        surface = mock.Mock(slot_count=10) if touch else None
        with mock.patch.object(phone_input, "log"):
            pi = phone_input.PhoneInput(touch_surface=surface, keyboard=board)
        self.addCleanup(pi.close)
        return pi, board

    def wait_for(self, board, count):
        done = threading.Event()
        for _ in range(200):
            if len(board.calls) >= count:
                done.set()
                break
            threading.Event().wait(0.01)
        self.assertTrue(done.is_set(), board.calls)

    def admit(self, pi):
        client = Client()
        with mock.patch.object(phone_input, "log"):
            pi.on_admitted(client)
        return client

    def test_input_state_says_keys(self):
        pi, _ = self.make()
        client = self.admit(pi)
        frame_type, body = client.sent[0]
        self.assertEqual(frame_type, phone_input.FRAME_INPUT_STATE)
        state = json.loads(body)
        self.assertTrue(state["keys"])
        self.assertFalse(state["touch"])

    def test_keys_frame_reaches_the_keyboard(self):
        pi, board = self.make()
        client = self.admit(pi)
        for keysym, pressed in ((XK_A, 1), (XK_A, 0)):
            pi.on_message(client, phone_input.FRAME_KEY,
                          phone_input.KEY_BODY.pack(keysym, pressed))
        self.wait_for(board, 2)
        self.assertEqual(board.calls, [(XK_A, True), (XK_A, False)])

    def test_bad_key_frames_are_ignored(self):
        pi, board = self.make()
        client = self.admit(pi)
        for body in (b"\x00", phone_input.KEY_BODY.pack(XK_a, 2),
                     phone_input.KEY_BODY.pack(0, 1),
                     phone_input.KEY_BODY.pack(0xFFFFFFFF, 1)):
            pi.on_message(client, phone_input.FRAME_KEY, body)
        stranger = Client()                       # never admitted
        pi.on_message(stranger, phone_input.FRAME_KEY,
                      phone_input.KEY_BODY.pack(XK_a, 1))
        threading.Event().wait(0.1)
        self.assertEqual(board.calls, [])

    def test_disconnect_lets_go_of_a_held_key(self):
        pi, board = self.make(touch=True)
        client = self.admit(pi)
        pi.on_message(client, phone_input.FRAME_KEY,
                      phone_input.KEY_BODY.pack(XK_Control_L, 1))
        pi.on_disconnect(client)
        self.wait_for(board, 2)
        self.assertEqual(board.calls, [(XK_Control_L, True),
                                       (XK_Control_L, False)])

    def test_without_keyboard_the_phone_is_told_no(self):
        with mock.patch.object(phone_input, "log"):
            pi = phone_input.PhoneInput(touch_surface=mock.Mock(slot_count=10))
        self.addCleanup(pi.close)
        client = self.admit(pi)
        self.assertFalse(json.loads(client.sent[0][1])["keys"])


class LayoutReaderTests(unittest.TestCase):
    def test_plasma_kxkbrc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "kxkbrc")
            with open(path, "w") as f:
                f.write("[Layout]\nLayoutList=de,us\nVariantList=nodeadkeys,\n"
                        "Options=caps:escape\nUse=true\n")
            self.assertEqual(kde.plasma_keyboard_layout(path),
                             {"layout": "de", "variant": "nodeadkeys",
                              "options": "caps:escape", "model": ""})
            self.assertEqual(kde.plasma_keyboard_layout(
                os.path.join(tmp, "missing")), {})

    def test_hyprland_options(self):
        answers = {"input:kb_layout": "fr,us", "input:kb_variant": "",
                   "input:kb_options": "grp:alt_shift_toggle",
                   "input:kb_model": "[[EMPTY]]"}
        with mock.patch.object(hyprland, "query",
                               lambda _g, name: {"str": answers[name]}):
            self.assertEqual(hyprland.hyprland_layout(),
                             {"layout": "fr", "variant": "",
                              "options": "grp:alt_shift_toggle", "model": ""})


class KwinKeyboardTests(unittest.TestCase):
    def test_keysyms_on_new_kwin(self):
        helper = mock.Mock(fake_input_version=6)
        board = kde.KwinKeyboard(helper)
        board.key(XK_a, True)
        board.key(XK_a, False)
        board.key(XK_a, False)                   # stray release: not sent
        self.assertEqual([c.args[0] for c in helper.send.call_args_list],
                         [f"keysym {XK_a} 1", f"keysym {XK_a} 0"])

    def test_key_codes_on_older_kwin(self):
        helper = mock.Mock(fake_input_version=5)
        with mock.patch.object(keyboard, "Keymap", lambda **_: FakeKeymap()):
            board = kde.KwinKeyboard(helper)
        board.key(XK_A, True)
        board.key(XK_A, False)
        self.assertEqual([c.args[0] for c in helper.send.call_args_list],
                         ["key 42 1", f"key {KEY_A} 1", f"key {KEY_A} 0",
                          "key 42 0"])

    def test_old_kwin_has_no_keyboard(self):
        backend = kde.KdeBackend.__new__(kde.KdeBackend)
        backend.helper = mock.Mock(alive=True, fake_input_version=3)
        with self.assertRaises(RuntimeError):
            backend.create_keyboard()


if __name__ == "__main__":
    unittest.main()
