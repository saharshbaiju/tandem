"""Tests for the niri backend's output lifecycle against an in-memory niri
(niri_fake.FakeNiri): creation, mode / scale, placement on every side with
the fall-back to the right when niri would move the user's monitors,
re-applying after a config reload, teardown and leftover cleanup."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import json
import os
import types
import unittest
from unittest import mock

from backends import gstreamer, niri
from niri_fake import FakeNiri, FakeOutput

FIXTURES = os.path.join(support.FIXTURES_DIR, "niri")


def args(**overrides):
    values = dict(width=2400, height=1080, fps=60, scale=1.5, side="right",
                  bitrate="12M", codec="h264", gop=30, encoder="auto",
                  touch=True, output="EXT-1", keep_output=False)
    values.update(overrides)
    return types.SimpleNamespace(**values)


class FakeNiriTestCase(unittest.TestCase):
    """Patches niri.request with a FakeNiri and collects log lines."""

    def use(self, fake):
        self.fake = fake
        self.logs = []
        patches = [
            mock.patch.object(niri, "request", fake.request),
            mock.patch.object(niri, "log",
                              lambda *a: self.logs.append(" ".join(a))),
            # The encoder's notes come from the shared GStreamer module.
            mock.patch.object(gstreamer, "log",
                              lambda *a: self.logs.append(" ".join(a))),
            mock.patch.dict(os.environ, {"NIRI_SOCKET": "/fake/niri.sock"}),
            # tandem.kdl lives in the scratch HOME; keep each test clean.
            mock.patch.object(niri, "clear_touch_mapping",
                              lambda path=None: False),
        ]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return fake

    def backend(self, **overrides):
        backend = niri.NiriBackend(args(**overrides))
        # The event watcher needs a real socket; tested separately.
        backend._watcher = mock.Mock()
        return backend

    def logged(self, text):
        return any(text in line for line in self.logs)


def laptop(position=None):
    """A real 1920x1080 monitor, auto-positioned unless `position`."""
    return FakeOutput("eDP-1", 1920, 1080, make="BOE", model="0x095F",
                      config_position=position)


class SetupTest(FakeNiriTestCase):
    def test_creates_the_output_with_mode_and_scale(self):
        self.use(FakeNiri([laptop()]))
        backend = self.backend()
        backend.setup()
        create = self.fake.sent("CreateVirtualOutput")
        self.assertEqual(create, [{"CreateVirtualOutput": {
            "width": 2400, "height": 1080, "refresh_rate": 60,
            "name": "Tandem"}}])
        tandem = self.fake.outputs["Tandem"].as_ipc()
        self.assertEqual(tandem["logical"]["scale"], 1.5)
        self.assertEqual((tandem["logical"]["width"],
                          tandem["logical"]["height"]), (1600, 720))
        backend._watcher.start.assert_called_once()

    def test_unsupported_niri_names_its_version_and_leaves_nothing(self):
        self.use(FakeNiri([laptop()], supports_virtual=False))
        with self.assertRaises(RuntimeError) as caught:
            self.backend().setup()
        message = str(caught.exception)
        self.assertIn("cannot create virtual monitors", message)
        self.assertIn(FakeNiri.VERSION, message)
        self.assertIn("#3800", message)
        self.assertEqual(list(self.fake.outputs), ["eDP-1"])

    def test_other_create_errors_are_reported(self):
        fake = self.use(FakeNiri([laptop()]))
        original = fake.request

        def refuse(payload, **kwargs):
            if isinstance(payload, dict) and "CreateVirtualOutput" in payload:
                raise niri.NiriError("virtual outputs are not supported on "
                                     "the Winit backend")
            return original(payload, **kwargs)

        with mock.patch.object(niri, "request", refuse):
            with self.assertRaisesRegex(RuntimeError, "Winit backend"):
                self.backend().setup()

    def test_needs_a_niri_session(self):
        self.use(FakeNiri([laptop()]))
        with mock.patch.dict(os.environ, {"NIRI_SOCKET": ""}):
            with self.assertRaisesRegex(RuntimeError, "NIRI_SOCKET"):
                self.backend().setup()

    def test_rejects_sizes_niri_cannot_take(self):
        self.use(FakeNiri([laptop()]))
        with self.assertRaisesRegex(RuntimeError, "1-65535"):
            self.backend(width=70000).setup()
        self.assertEqual(self.fake.sent("CreateVirtualOutput"), [])

    def test_removes_a_leftover_from_a_killed_daemon_first(self):
        fake = FakeNiri([laptop(), FakeOutput("Tandem", 1280, 720,
                                              virtual=True)])
        fake.workspaces = [
            {"id": 1, "idx": 1, "output": "eDP-1", "active_window_id": 7},
            {"id": 4, "idx": 1, "output": "Tandem", "active_window_id": 9},
            {"id": 5, "idx": 2, "output": "Tandem", "active_window_id": None},
        ]
        fake.windows = [{"id": 7, "workspace_id": 1},
                        {"id": 9, "workspace_id": 4}]
        self.use(fake)
        self.backend().setup()
        moves = self.fake.sent("Action")
        self.assertEqual(moves, [{"Action": {"MoveWorkspaceToMonitor": {
            "output": "eDP-1", "reference": {"Id": 4}}}}])
        self.assertEqual(self.fake.sent("RemoveVirtualOutput"),
                         [{"RemoveVirtualOutput": {"name": "Tandem"}}])
        self.assertEqual(self.fake.outputs["Tandem"].mode[:2], (2400, 1080))
        self.assertTrue(self.logged("left behind"))

    def test_refuses_an_output_named_tandem_that_is_not_ours(self):
        self.use(FakeNiri([laptop(), FakeOutput("Tandem", 1920, 1080,
                                                make="Acme", model="X")]))
        with self.assertRaisesRegex(RuntimeError, "didn't make"):
            self.backend().setup()
        self.assertEqual(self.fake.sent("RemoveVirtualOutput"), [])

    def test_enforces_the_mode_over_a_config_mode_block(self):
        self.use(FakeNiri([laptop()],
                          config_mode_for_tandem=(1280, 720, 30000)))
        self.backend().setup()
        self.assertEqual(self.fake.outputs["Tandem"].current_mode(),
                         (2400, 1080, 60000))
        self.assertEqual(self.fake.sent("Output")[0]["Output"]["action"],
                         {"CustomMode": {"mode": {"width": 2400,
                                                  "height": 1080,
                                                  "refresh": 60.0}}})

    def test_cleans_up_when_placement_fails(self):
        fake = self.use(FakeNiri([laptop()]))
        original = fake.request

        def refuse_scale(payload, **kwargs):
            if isinstance(payload, dict) and "Output" in payload:
                raise niri.NiriError("invalid scale")
            return original(payload, **kwargs)

        with mock.patch.object(niri, "request", refuse_scale):
            with self.assertRaisesRegex(RuntimeError, "invalid scale"):
                self.backend().setup()
        self.assertNotIn("Tandem", self.fake.outputs)


class PlacementTest(FakeNiriTestCase):
    def place(self, side, monitor):
        self.use(FakeNiri([monitor]))
        backend = self.backend(side=side)
        backend.setup()
        return backend

    def test_right_is_automatic_and_never_moves_the_monitor(self):
        self.place("right", laptop())
        self.assertEqual(self.fake.position_of("Tandem"), (1920, 0))
        self.assertEqual(self.fake.position_of("eDP-1"), (0, 0))
        self.assertEqual(
            self.fake.sent("Output")[-1]["Output"]["action"],
            {"Position": {"position": "Automatic"}})

    def test_left_works_with_an_automatic_monitor(self):
        # niri places Tandem first (explicit) and the laptop after it at x=0.
        self.place("left", laptop())
        self.assertEqual(self.fake.position_of("Tandem"), (-1600, 0))
        self.assertEqual(self.fake.position_of("eDP-1"), (0, 0))
        self.assertFalse(self.logged("warning"))

    def test_up_with_an_automatic_monitor_falls_back_to_the_right(self):
        self.place("up", laptop())
        self.assertEqual(self.fake.position_of("eDP-1"), (0, 0))
        self.assertEqual(self.fake.position_of("Tandem"), (1920, 0))
        self.assertTrue(self.logged("moved eDP-1"))
        self.assertTrue(self.logged("putting it on the right instead"))
        self.assertTrue(self.logged("placed Tandem side=right"))
        self.assertFalse(self.logged("placed Tandem side=up"))

    def test_down_with_an_automatic_monitor_falls_back_to_the_right(self):
        self.place("down", laptop())
        self.assertEqual(self.fake.position_of("eDP-1"), (0, 0))
        self.assertEqual(self.fake.position_of("Tandem"), (1920, 0))

    def test_up_and_down_with_an_explicit_monitor(self):
        self.place("up", laptop(position=(0, 0)))
        self.assertEqual(self.fake.position_of("Tandem"), (0, -720))
        self.assertEqual(self.fake.position_of("eDP-1"), (0, 0))
        self.assertFalse(self.logged("warning"))

        self.place("down", laptop(position=(0, 0)))
        self.assertEqual(self.fake.position_of("Tandem"), (0, 1080))

    def test_a_monitor_turned_sideways_is_placed_by_its_turned_size(self):
        # niri reports `logical` after the transform, flipped or not, so a
        # 1920x1080 panel turned a quarter is 1080 wide and 1920 tall.
        for transform in ("90", "270", "Flipped90", "Flipped270"):
            portrait = FakeOutput("DP-1", 1920, 1080, make="Dell Inc.",
                                  config_position=(0, 0),
                                  transform=transform)
            with self.subTest(transform=transform, side="down"):
                self.place("down", portrait)
                self.assertEqual(self.fake.position_of("Tandem"), (0, 1920))
            with self.subTest(transform=transform, side="right"):
                self.place("right", portrait)
                self.assertEqual(self.fake.position_of("Tandem"), (1080, 0))
        for transform in ("Normal", "180", "Flipped", "Flipped180"):
            landscape = FakeOutput("DP-1", 1920, 1080, make="Dell Inc.",
                                   config_position=(0, 0),
                                   transform=transform)
            with self.subTest(transform=transform):
                self.place("down", landscape)
                self.assertEqual(self.fake.position_of("Tandem"), (0, 1080))

    def test_left_beside_a_monitor_that_is_not_at_the_origin(self):
        self.place("left", laptop(position=(500, 200)))
        self.assertEqual(self.fake.position_of("Tandem"), (-1100, 200))
        self.assertEqual(self.fake.position_of("eDP-1"), (500, 200))

    def test_apply_live_moves_it_and_sends_nothing_when_already_there(self):
        backend = self.place("right", laptop(position=(0, 0)))
        self.assertTrue(backend.apply_live("left", 1.5))
        self.assertEqual(self.fake.position_of("Tandem"), (-1600, 0))
        sent = len(self.fake.sent("Output"))
        self.assertTrue(backend.apply_live("left", 1.5))
        self.assertEqual(len(self.fake.sent("Output")), sent)
        self.assertTrue(backend.apply_live("left", 2.0))
        self.assertEqual(self.fake.outputs["Tandem"].as_ipc()["logical"]
                         ["width"], 1200)
        self.assertEqual(self.fake.position_of("Tandem"), (-1200, 0))

    def test_scale_change_on_the_left_keeps_an_automatic_monitor(self):
        # The old explicit spot (for the old size) would overlap the laptop
        # at the new size, and niri would push the laptop aside.
        backend = self.place("left", laptop())
        for scale, x in ((2.0, -1200), (1.5, -1600), (1.0, -2400)):
            self.assertTrue(backend.apply_live("left", scale))
            self.assertEqual(self.fake.position_of("Tandem"), (x, 0))
            self.assertEqual(self.fake.position_of("eDP-1"), (0, 0))
        self.assertFalse(self.logged("warning"))

    def test_apply_live_reports_a_vanished_output(self):
        backend = self.place("right", laptop())
        del self.fake.outputs["Tandem"]
        self.assertFalse(backend.apply_live("left", 1.5))
        self.assertTrue(self.logged("gone"))

    def test_config_reload_is_reapplied(self):
        backend = self.place("left", laptop(position=(0, 0)))
        self.fake.reload_config()
        self.assertEqual(self.fake.outputs["Tandem"].scale(), 1.0)
        backend._reapply()
        self.assertEqual(self.fake.outputs["Tandem"].scale(), 1.5)
        self.assertEqual(self.fake.position_of("Tandem"), (-1600, 0))

    def test_reload_resends_automatic_even_if_it_looks_right(self):
        backend = self.place("right", laptop())
        before = len(self.fake.sent("Output"))
        backend._reapply()
        self.assertEqual(self.fake.sent("Output")[before:],
                         [{"Output": {"output": "Tandem", "action": {
                             "Position": {"position": "Automatic"}}}}])

    def test_reapply_does_nothing_after_teardown(self):
        backend = self.place("right", laptop())
        backend.teardown()
        requests = len(self.fake.requests)
        backend._reapply()
        self.assertEqual(len(self.fake.requests), requests)


class TeardownTest(FakeNiriTestCase):
    def test_moves_windows_back_by_id_and_removes_the_output(self):
        self.use(FakeNiri([laptop()]))
        backend = self.backend()
        backend.setup()
        self.fake.workspaces = [
            {"id": 1, "idx": 1, "output": "eDP-1", "active_window_id": 2},
            {"id": 3, "idx": 1, "output": "Tandem", "active_window_id": 8},
            {"id": 6, "idx": 2, "output": "Tandem", "active_window_id": None},
        ]
        self.fake.windows = [{"id": 2, "workspace_id": 1},
                             {"id": 8, "workspace_id": 3}]
        backend.teardown()
        self.assertEqual(self.fake.sent("Action"), [{"Action": {
            "MoveWorkspaceToMonitor": {"output": "eDP-1",
                                       "reference": {"Id": 3}}}}])
        self.assertNotIn("Tandem", self.fake.outputs)
        backend._watcher.stop.assert_called_once()
        requests = len(self.fake.requests)
        backend.teardown()                  # idempotent
        self.assertEqual(len(self.fake.requests), requests)

    def test_never_raises(self):
        self.use(FakeNiri([laptop()]))
        backend = self.backend()
        backend.setup()
        with mock.patch.object(niri, "request",
                               side_effect=RuntimeError("niri is gone")):
            backend.teardown()
        self.assertTrue(self.logged("could not remove Tandem"))

    def test_clears_the_touch_mapping_it_wrote(self):
        self.use(FakeNiri([laptop()]))
        backend = self.backend()
        backend.setup()
        backend._touch_mapped = True
        with mock.patch.object(niri, "clear_touch_mapping") as clear:
            backend.teardown()
        clear.assert_called_once_with()

    def test_stops_capture_first(self):
        self.use(FakeNiri([laptop()]))
        backend = self.backend()
        backend.setup()
        order = []
        backend.stop_capture = lambda: order.append("capture")
        original = niri.remove_tandem_output
        with mock.patch.object(
                niri, "remove_tandem_output",
                lambda *a: order.append("remove") or original(*a)):
            backend.teardown()
        self.assertEqual(order, ["capture", "remove"])


class LeftoverHookTest(FakeNiriTestCase):
    def test_removes_only_our_output(self):
        self.use(FakeNiri([laptop(), FakeOutput("Tandem", 2400, 1080,
                                                virtual=True)]))
        niri.remove_leftover_output("EXT-1")    # the TUI's Hyprland name
        self.assertEqual(list(self.fake.outputs), ["eDP-1"])

    def test_leaves_a_foreign_output_alone(self):
        self.use(FakeNiri([laptop(), FakeOutput("Tandem", 1920, 1080,
                                                make="Acme", model="X")]))
        niri.remove_leftover_output("EXT-1")
        self.assertIn("Tandem", self.fake.outputs)

    def test_never_raises_without_niri(self):
        with mock.patch.dict(os.environ, {"NIRI_SOCKET": "/nonexistent"}):
            niri.remove_leftover_output("EXT-1")


class CaptureTest(FakeNiriTestCase):
    def test_refuses_when_the_output_is_gone(self):
        self.use(FakeNiri([laptop()]))
        backend = self.backend()
        with mock.patch.object(gstreamer, "pick_encoder", return_value="x264enc"):
            with self.assertRaisesRegex(RuntimeError, "is gone"):
                backend.start_capture()

    def test_starts_a_screencast_and_the_pipeline(self):
        self.use(FakeNiri([laptop()]))
        backend = self.backend()
        backend.setup()
        cast = mock.Mock()
        cast.start.return_value = 42
        proc, stream = mock.Mock(), mock.Mock()
        with mock.patch.object(gstreamer, "pick_encoder", return_value="x264enc"), \
                mock.patch.object(niri, "ScreenCastSession",
                                  return_value=cast) as session_class, \
                mock.patch.object(gstreamer, "build_pipeline",
                                  return_value=["gst"]) as build, \
                mock.patch.object(gstreamer, "spawn_pipeline",
                                  side_effect=lambda describe:
                                  (describe(5) and (proc, stream))):
            self.assertIs(backend.start_capture(), stream)
        session_class.assert_called_once_with("Tandem")
        build.assert_called_once_with(42, 2400, 1080, 12000, 30, "x264enc",
                                      fd=5, fps=60)
        with mock.patch.object(niri, "stop_pipeline") as stop:
            backend.stop_capture()
            backend.stop_capture()          # idempotent
        stop.assert_has_calls([mock.call(proc), mock.call(None)])
        cast.close.assert_called_once_with()

    def test_restores_a_mode_a_config_reload_reset(self):
        self.use(FakeNiri([laptop()]))
        backend = self.backend()
        backend.setup()
        self.fake.reload_config()
        self.fake.outputs["Tandem"].mode = (1280, 720, 30000)  # config mode
        cast = mock.Mock()
        cast.start.return_value = 42
        with mock.patch.object(gstreamer, "pick_encoder", return_value="x264enc"), \
                mock.patch.object(niri, "ScreenCastSession",
                                  return_value=cast), \
                mock.patch.object(gstreamer, "spawn_pipeline",
                                  return_value=(mock.Mock(), mock.Mock())):
            backend.start_capture()
        tandem = self.fake.outputs["Tandem"]
        self.assertEqual(tandem.current_mode(), (2400, 1080, 60000))
        self.assertEqual(tandem.scale(), 1.5)

    def test_closes_the_screencast_when_gst_cannot_start(self):
        self.use(FakeNiri([laptop()]))
        backend = self.backend()
        backend.setup()
        cast = mock.Mock()
        cast.start.return_value = 42
        with mock.patch.object(gstreamer, "pick_encoder", return_value="x264enc"), \
                mock.patch.object(niri, "ScreenCastSession",
                                  return_value=cast), \
                mock.patch.object(gstreamer, "spawn_pipeline",
                                  side_effect=FileNotFoundError("gst")):
            with self.assertRaises(FileNotFoundError):
                backend.start_capture()
        cast.close.assert_called_once_with()
        self.assertIsNone(backend.cast)

    def test_needs_an_encoder(self):
        self.use(FakeNiri([laptop()]))
        with mock.patch.object(gstreamer, "pick_encoder", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "encoder"):
                self.backend().start_capture()


class FixtureParsingTest(unittest.TestCase):
    """Real `niri msg --json` replies captured from the PR #3800 build."""

    def load(self, name):
        with open(os.path.join(FIXTURES, name)) as f:
            return json.load(f)

    def test_virtual_outputs_are_recognised(self):
        outputs = self.load("outputs-with-tandem-virtual.json")
        self.assertTrue(niri.is_tandem_output(outputs["Tandem"]))
        self.assertFalse(niri.is_tandem_output(outputs["HEADLESS-1"]))
        self.assertTrue(niri.is_virtual(outputs["HEADLESS-1"]))
        winit = self.load("outputs-stable-winit.json")["winit"]
        self.assertFalse(niri.is_virtual(winit))

    def test_geometry_and_mode(self):
        tandem = self.load("outputs-with-tandem-virtual.json")["Tandem"]
        self.assertEqual(niri.logical_rect(tandem), (1920, 0, 1600, 720))
        self.assertEqual(niri.current_mode(tandem), (2400, 1080, 60000))
        self.assertIsNone(niri.logical_rect({"logical": None}))
        self.assertIsNone(niri.current_mode({"modes": [],
                                             "current_mode": None}))

    def test_anchor_is_the_monitor_at_the_origin_and_never_ours(self):
        outputs = self.load("outputs-headless-default.json")
        self.assertEqual(niri.pick_anchor(outputs)["name"], "HEADLESS-1")
        moved = self.load("outputs-with-tandem-virtual.json")
        # HEADLESS-1 was pushed to x=3520 by an explicit right position:
        # with nothing at 0,0 the top-left other output is the anchor.
        self.assertEqual(niri.pick_anchor(moved)["name"], "HEADLESS-1")
        self.assertIsNone(niri.pick_anchor({"Tandem": moved["Tandem"]}))

    def test_real_monitors_are_preferred_as_anchor(self):
        outputs = self.load("outputs-headless-default.json")
        winit = self.load("outputs-stable-winit.json")["winit"]
        winit = dict(winit, logical=dict(winit["logical"], x=1920))
        outputs["winit"] = winit
        self.assertEqual(niri.pick_anchor(outputs)["name"], "winit")

    def test_explicit_positions_for_each_side(self):
        outputs = self.load("outputs-headless-default.json")
        size = (1600, 720)
        self.assertIsNone(niri.explicit_position("right", outputs, size))
        self.assertEqual(niri.explicit_position("left", outputs, size),
                         (-1600, 0))
        self.assertEqual(niri.explicit_position("up", outputs, size),
                         (0, -720))
        self.assertEqual(niri.explicit_position("down", outputs, size),
                         (0, 1080))
        self.assertIsNone(niri.explicit_position("left", {}, size))

    def test_moved_outputs(self):
        before = niri.positions_of_others(
            self.load("outputs-headless-default.json"))
        after = niri.positions_of_others(
            self.load("outputs-with-tandem-virtual.json"))
        self.assertEqual(before, {"HEADLESS-1": (0, 0)})
        self.assertEqual(niri.moved_outputs(before, after), ["HEADLESS-1"])
        self.assertEqual(niri.moved_outputs(before, before), [])
        self.assertEqual(niri.moved_outputs(before, {}), [])

    def test_workspaces_with_windows(self):
        workspaces = []
        with open(os.path.join(FIXTURES, "workspaces-with-tandem.jsonl")) as f:
            workspaces = [json.loads(line) for line in f if line.strip()]
        windows = self.load("windows-with-tandem.json")
        replies = {"Workspaces": {"Workspaces": workspaces},
                   "Windows": {"Windows": windows}}
        with mock.patch.object(niri, "request",
                               lambda name, **kw: replies[name]):
            self.assertEqual(niri._workspaces_with_windows_on("Tandem"), [2])
            self.assertEqual(niri._workspaces_with_windows_on("HEADLESS-1"),
                             [1])

    def test_describe_primary_monitor(self):
        outputs = self.load("outputs-with-tandem-virtual.json")
        with mock.patch.object(niri, "outputs", return_value=outputs):
            self.assertEqual(niri.describe_primary_monitor(),
                             "HEADLESS-1  1920x1080@60  at 3520,0  scale 1")
        with mock.patch.object(niri, "outputs", return_value=None):
            self.assertIsNone(niri.describe_primary_monitor())


class ClassContractTest(unittest.TestCase):
    def test_contract(self):
        self.assertEqual(niri.NiriBackend.name, "niri")
        self.assertFalse(niri.NiriBackend.honors_keep_output)
        for method in ("missing", "setup", "start_capture", "stop_capture",
                       "apply_live", "create_touch_surface", "teardown"):
            self.assertTrue(callable(getattr(niri.NiriBackend, method)))

    def test_constructor_does_not_touch_niri(self):
        with mock.patch.object(niri, "request") as request:
            niri.NiriBackend(args())
        request.assert_not_called()

    def test_missing_reports_gi_and_gstreamer(self):
        with mock.patch.object(gstreamer.shutil, "which", return_value=None), \
                mock.patch.object(niri, "Gio", None):
            self.assertEqual(niri.NiriBackend.missing(),
                             ["gst-launch-1.0", "gst-inspect-1.0",
                              "python-gobject (gi)"])


if __name__ == "__main__":
    unittest.main()
