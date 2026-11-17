"""Tests for pointer mode, where the phone acts as a mouse: the PNTR frame and
PointerRouter (phone_input.py), Hyprland's layout box (backends/hyprland.py),
the KWin helper commands (backends/kde.py) and the uinput position maths
(uinput_pointer.py). No input device is created.
"""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import json
import threading
import unittest
from unittest import mock

import phone_input
import uinput_pointer
from backends import hyprland, kde

LEFT, RIGHT = 0x110, 0x111


class RecordingPointer:
    def __init__(self):
        self.calls = []

    def move(self, x, y):
        self.calls.append(("move", round(x, 3), round(y, 3)))

    def button(self, code, pressed):
        self.calls.append(("button", code, pressed))

    def scroll(self, dx, dy):
        self.calls.append(("scroll", dx, dy))


class Client:
    alive = True
    addr = ("192.0.2.1", 1)

    def __init__(self):
        self.sent = []

    def send_message(self, frame_type, body):
        self.sent.append((frame_type, body))


def pntr(action, button=0, x=0.0, y=0.0, dx=0, dy=0):
    return phone_input.POINTER_BODY.pack(action, button, round(x * 65535),
                                         round(y * 65535), dx, dy)


class PointerRouterTest(unittest.TestCase):
    def test_click_moves_then_presses(self):
        board = RecordingPointer()
        router = phone_input.PointerRouter(board)
        phone = object()
        router.handle(phone, phone_input.POINTER_DOWN, 1, 0.5, 0.25, 0, 0)
        router.handle(phone, phone_input.POINTER_UP, 1, 0.5, 0.25, 0, 0)
        self.assertEqual(board.calls, [("move", 0.5, 0.25), ("button", LEFT, True),
                                       ("move", 0.5, 0.25), ("button", LEFT, False)])

    def test_disconnect_lets_go_of_a_drag(self):
        board = RecordingPointer()
        router = phone_input.PointerRouter(board)
        phone = object()
        router.handle(phone, phone_input.POINTER_DOWN, 1, 0.1, 0.1, 0, 0)
        board.calls.clear()
        router.client_gone(phone)
        self.assertEqual(board.calls, [("button", LEFT, False)])

    def test_unknown_button_only_moves_and_stray_up_is_dropped(self):
        board = RecordingPointer()
        router = phone_input.PointerRouter(board)
        router.handle(object(), phone_input.POINTER_DOWN, 9, 0.2, 0.2, 0, 0)
        router.handle(object(), phone_input.POINTER_UP, 2, 0.2, 0.2, 0, 0)
        self.assertEqual([c[0] for c in board.calls], ["move", "move"])


class PhoneInputPointerTest(unittest.TestCase):
    def setUp(self):
        self.board = RecordingPointer()
        with mock.patch.object(phone_input, "log"):
            self.pi = phone_input.PhoneInput(pointer=self.board)
        self.addCleanup(self.pi.close)
        self.client = Client()
        with mock.patch.object(phone_input, "log"):
            self.pi.on_admitted(self.client)

    def wait_for(self, count):
        for _ in range(200):
            if len(self.board.calls) >= count:
                return
            threading.Event().wait(0.01)
        self.fail(self.board.calls)

    def test_input_state_offers_pointer_mode(self):
        state = json.loads(self.client.sent[0][1])
        self.assertTrue(state["pointer"])
        self.assertFalse(state["touch"])

    def test_frames_reach_the_pointer_in_order(self):
        for body in (pntr(phone_input.POINTER_DOWN, 2, 0.5, 0.5),
                     pntr(phone_input.POINTER_UP, 2, 0.5, 0.5),
                     pntr(phone_input.POINTER_SCROLL, dy=-240)):
            self.pi.on_message(self.client, phone_input.FRAME_POINTER, body)
        self.wait_for(5)
        self.assertEqual(self.board.calls, [
            ("move", 0.5, 0.5), ("button", RIGHT, True),
            ("move", 0.5, 0.5), ("button", RIGHT, False),
            ("scroll", 0, -240)])

    def test_bad_frames_are_ignored(self):
        self.pi.on_message(self.client, phone_input.FRAME_POINTER, b"\x00")
        self.pi.on_message(self.client, phone_input.FRAME_POINTER, pntr(7))
        self.pi.on_message(Client(), phone_input.FRAME_POINTER,
                           pntr(phone_input.POINTER_DOWN, 1))
        threading.Event().wait(0.1)
        self.assertEqual(self.board.calls, [])

    def test_disconnect_mid_drag_releases_the_button(self):
        self.pi.on_message(self.client, phone_input.FRAME_POINTER,
                           pntr(phone_input.POINTER_DOWN, 1, 0.3, 0.3))
        self.pi.on_disconnect(self.client)
        self.wait_for(3)
        self.assertEqual(self.board.calls[-1], ("button", LEFT, False))


class LayoutGeometryTest(unittest.TestCase):
    MONS = [
        {"name": "eDP-1", "x": 0, "y": 0, "width": 2560, "height": 1600,
         "scale": 1.6, "transform": 0},
        {"name": "Tandem-1", "x": 1600, "y": 100, "width": 2400,
         "height": 1080, "scale": 1.5, "transform": 0},
        {"name": "HDMI-A-1", "x": -1920, "y": 0, "width": 1920,
         "height": 1080, "scale": 1, "transform": 0, "disabled": True},
    ]

    def test_box_spans_active_monitors_and_finds_ours(self):
        box, own = hyprland.layout_geometry("Tandem-1", self.MONS)
        self.assertEqual(box, (0, 0, 3200, 1000))
        self.assertEqual(own, (1600, 100, 1600, 720))

    def test_missing_output(self):
        self.assertIsNone(hyprland.layout_geometry("Nope", self.MONS))


class UinputPositionTest(unittest.TestCase):
    def test_fraction_of_our_monitor_becomes_a_box_position(self):
        pointer = uinput_pointer.VirtualPointer.__new__(uinput_pointer.VirtualPointer)
        pointer._geometry = lambda: ((0, 0, 3201, 1001), (1600, 100, 1601, 721))
        events = pointer._position(1.0, 0.5)
        x = events[0][2] / uinput_pointer.ABS_MAX * 3200
        y = events[1][2] / uinput_pointer.ABS_MAX * 1000
        self.assertAlmostEqual(x, 3200, delta=0.1)
        self.assertAlmostEqual(y, 460, delta=0.1)


class KwinPointerTest(unittest.TestCase):
    def test_commands(self):
        helper = mock.Mock(output_name="Virtual-Tandem")
        pointer = kde.KwinPointer(helper)
        pointer.move(0.5, 0.25)
        pointer.button(LEFT, True)
        pointer.scroll(0, 120)
        pointer.close()
        sent = [c.args[0] for c in helper.send.call_args_list]
        self.assertEqual(sent[0].split()[0], "pmove")
        self.assertEqual(sent[1:], [f"button {LEFT} 1", "axis 0 15.000",
                                    f"button {LEFT} 0"])

    def test_old_kwin_has_no_pointer(self):
        backend = kde.KdeBackend.__new__(kde.KdeBackend)
        backend.helper = mock.Mock(alive=True, fake_input_version=2)
        with self.assertRaises(RuntimeError):
            backend.create_pointer()


if __name__ == "__main__":
    unittest.main()
