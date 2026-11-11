"""Tests for the KDE backend's kscreen-doctor parsing and placement planning
(backends/kde.py), with JSON recorded from KWin 6.8.2 and 6.7.5."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import json
import os
import subprocess
import unittest
from unittest import mock

from backends import kde

FIXTURES = os.path.join(support.FIXTURES_DIR, "kde")
TANDEM = "Virtual-Tandem"
MAIN = "Virtual-0"


def fixture_text(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fixture:
        return fixture.read()


def fixture_outputs(name):
    return kde.parse_outputs(fixture_text(name))


def output(name, x=0, y=0, width=1920, height=1080, scale=1.0, rotation=1,
           priority=0, enabled=True, connected=True, refresh=60.0):
    return kde.KscreenOutput(name, enabled, connected, x, y, width, height,
                             scale, rotation, priority, refresh)


def by_name(outputs):
    return {item.name: item for item in outputs}


class ParseOutputsTest(unittest.TestCase):
    def test_kwin682_virtual_output_without_enabled_key_counts_as_enabled(self):
        outputs = by_name(fixture_outputs(
            "kscreen-doctor-kwin682-virtual-placed.json"))
        self.assertEqual(set(outputs), {TANDEM, MAIN})
        tandem = outputs[TANDEM]
        self.assertTrue(tandem.enabled)
        self.assertTrue(tandem.active)
        self.assertEqual((tandem.x, tandem.y), (1920, 0))
        self.assertEqual((tandem.width, tandem.height), (2400, 1080))
        self.assertEqual(tandem.scale, 1.5)
        self.assertEqual(tandem.logical_size(), (1600, 720))
        self.assertEqual(outputs[MAIN].priority, 1)
        self.assertEqual(outputs[MAIN].rect(), (0, 0, 1920, 1080))

    def test_kwin675_reports_enabled_and_refresh(self):
        outputs = by_name(fixture_outputs(
            "kscreen-doctor-kwin675-virtual-placed.json"))
        self.assertTrue(outputs[TANDEM].enabled)
        self.assertEqual(outputs[TANDEM].priority, 2)
        self.assertGreater(outputs[MAIN].refresh, 0)

    def test_single_output_session_has_no_primary_priority(self):
        (only,) = fixture_outputs("kscreen-doctor-kwin682-single-output.json")
        self.assertEqual(only.name, MAIN)
        self.assertEqual(only.priority, 0)

    def test_unreadable_input_gives_an_empty_list(self):
        for text in ("", "not json", "[]", '{"outputs": null}', None):
            with self.subTest(text=text):
                self.assertEqual(kde.parse_outputs(text), [])

    def test_malformed_and_sizeless_entries_are_skipped(self):
        text = json.dumps({"outputs": [
            {"name": "DP-1", "enabled": False, "connected": True},   # no size
            {"name": "HDMI-1", "size": {"width": "wide", "height": 1}},
            {"size": {"width": 10, "height": 10}},                    # no name
            {"name": "eDP-1", "pos": {"x": 5, "y": 6},
             "size": {"width": 2560, "height": 1600}, "scale": 1.25,
             "connected": True, "enabled": True, "priority": 1},
        ]})
        (laptop,) = kde.parse_outputs(text)
        self.assertEqual((laptop.name, laptop.x, laptop.y), ("eDP-1", 5, 6))
        self.assertEqual(laptop.logical_size(), (2048, 1280))

    def test_sideways_rotation_swaps_the_logical_size(self):
        # libkscreen: None 1, Left 2, Inverted 4, Right 8, Flipped 16,
        # Flipped90 32, Flipped180 64, Flipped270 128.
        for rotation, size in ((1, (1920, 1080)), (2, (1080, 1920)),
                               (4, (1920, 1080)), (8, (1080, 1920)),
                               (16, (1920, 1080)), (32, (1080, 1920)),
                               (64, (1920, 1080)), (128, (1080, 1920))):
            with self.subTest(rotation=rotation):
                self.assertEqual(output(MAIN, rotation=rotation).logical_size(),
                                 size)


class PlanLayoutTest(unittest.TestCase):
    def setUp(self):
        self.placed = fixture_outputs(
            "kscreen-doctor-kwin682-virtual-placed.json")
        self.left = fixture_outputs("kscreen-doctor-kwin682-virtual-left.json")

    def fresh(self):
        """Our output just created: KWin put it right of the main monitor
        (its default spot) at scale 1."""
        return [output(MAIN, priority=1),
                output(TANDEM, x=1920, width=2400, height=1080, priority=2)]

    def test_new_output_gets_its_scale_and_stays_right_of_the_main_monitor(self):
        plan = kde.plan_layout(self.fresh(), TANDEM, "right", 1.5)
        self.assertEqual(plan.arguments, [f"output.{TANDEM}.scale.1.5"])
        self.assertEqual(plan.positions, {MAIN: (0, 0), TANDEM: (1920, 0)})
        self.assertEqual(plan.shifted, (0, 0))

    def test_nothing_is_sent_when_the_layout_is_already_right(self):
        plan = kde.plan_layout(self.placed, TANDEM, "right", 1.5)
        self.assertEqual(plan.arguments, [])
        self.assertTrue(kde.layout_matches(self.placed, plan, TANDEM))
        plan = kde.plan_layout(self.left, TANDEM, "left", 1.5)
        self.assertEqual(plan.arguments, [])

    def test_left_moves_the_other_monitors_right_instead_of_going_negative(self):
        plan = kde.plan_layout(self.placed, TANDEM, "left", 1.5)
        self.assertEqual(plan.arguments, [f"output.{MAIN}.position.1600,0",
                                          f"output.{TANDEM}.position.0,0"])
        self.assertEqual(plan.shifted, (1600, 0))
        self.assertEqual(plan.positions, {MAIN: (1600, 0), TANDEM: (0, 0)})

    def test_moving_back_right_undoes_the_shift(self):
        plan = kde.plan_layout(self.left, TANDEM, "right", 1.5)
        self.assertEqual(plan.arguments, [f"output.{MAIN}.position.0,0",
                                          f"output.{TANDEM}.position.1920,0"])
        self.assertEqual(plan.shifted, (-1600, 0))

    def test_up_and_down(self):
        up = kde.plan_layout(self.placed, TANDEM, "up", 1.5)
        self.assertEqual(up.positions, {MAIN: (0, 720), TANDEM: (0, 0)})
        down = kde.plan_layout(self.placed, TANDEM, "down", 1.5)
        self.assertEqual(down.positions, {MAIN: (0, 0), TANDEM: (0, 1080)})
        self.assertEqual(down.shifted, (0, 0))

    def test_every_other_monitor_moves_by_the_same_offset(self):
        outputs = [output("DP-1", priority=1),
                   output("DP-2", x=1920, width=2560, height=1440),
                   output(TANDEM, x=4480, width=2400, height=1080)]
        plan = kde.plan_layout(outputs, TANDEM, "left", 1.5)
        self.assertEqual(plan.positions, {"DP-1": (1600, 0),
                                          "DP-2": (3520, 0), TANDEM: (0, 0)})

    def test_the_phone_is_never_left_primary(self):
        outputs = [output(MAIN, priority=2),
                   output(TANDEM, x=1920, width=2400, height=1080,
                          scale=1.5, priority=1)]
        plan = kde.plan_layout(outputs, TANDEM, "right", 1.5)
        self.assertEqual(plan.arguments, [f"output.{MAIN}.priority.1"])

    def test_anchor_is_the_top_left_monitor_without_a_primary(self):
        outputs = [output("DP-2", x=1920, priority=0),
                   output("DP-1", x=0, priority=0),
                   output(TANDEM, x=3840, width=2400, height=1080)]
        plan = kde.plan_layout(outputs, TANDEM, "down", 1.0)
        self.assertEqual(plan.positions[TANDEM], (0, 1080))

    def test_a_monitor_flipped_sideways_is_measured_rotated(self):
        # 'Flipped 90°' (libkscreen 32) on a 1920x1080 panel: 1080x1920.
        outputs = [output(MAIN, rotation=32, priority=1),
                   output(TANDEM, x=1920, width=2400, height=1080)]
        right = kde.plan_layout(outputs, TANDEM, "right", 1.0)
        self.assertEqual(right.positions[TANDEM], (1080, 0))
        down = kde.plan_layout(outputs, TANDEM, "down", 1.0)
        self.assertEqual(down.positions[TANDEM], (0, 1920))

    def test_another_tandem_output_is_never_the_anchor(self):
        outputs = [output("Virtual-Tandem-2", x=0, width=800, height=600),
                   output(MAIN, x=800),
                   output(TANDEM, x=2720, width=2400, height=1080)]
        plan = kde.plan_layout(outputs, TANDEM, "down", 1.0)
        self.assertEqual(plan.positions[TANDEM], (800, 1080))

    def test_disabled_and_disconnected_outputs_are_ignored(self):
        outputs = [output(MAIN, priority=1),
                   output("HDMI-1", x=-5000, enabled=False),
                   output("DP-3", x=1920, connected=False),
                   output(TANDEM, x=3000, width=2400, height=1080)]
        plan = kde.plan_layout(outputs, TANDEM, "right", 1.0)
        self.assertEqual(plan.positions, {MAIN: (0, 0), TANDEM: (1920, 0)})
        self.assertNotIn("HDMI-1", " ".join(plan.arguments))

    def test_alone_it_goes_to_the_origin(self):
        plan = kde.plan_layout([output(TANDEM, x=50, y=50)], TANDEM, "left",
                               1.0)
        self.assertEqual(plan.positions, {TANDEM: (0, 0)})
        self.assertEqual(plan.shifted, (0, 0))

    def test_scale_is_snapped_to_whole_logical_pixels(self):
        outputs = [output(MAIN, priority=1),
                   output(TANDEM, x=1920, width=2772, height=1280)]
        plan = kde.plan_layout(outputs, TANDEM, "right", 1.5)
        self.assertAlmostEqual(plan.scale, 4 / 3, places=5)
        self.assertIn(f"output.{TANDEM}.scale.1.333333", plan.arguments)

    def test_unknown_output_raises(self):
        with self.assertRaises(RuntimeError):
            kde.plan_layout(self.placed, "Virtual-Nope", "right", 1.0)

    def test_layout_matches_detects_a_different_layout(self):
        plan = kde.plan_layout(self.placed, TANDEM, "left", 1.5)
        self.assertFalse(kde.layout_matches(self.placed, plan, TANDEM))
        self.assertTrue(kde.layout_matches(self.left, plan, TANDEM))
        self.assertFalse(kde.layout_matches([], plan, TANDEM))

    def test_format_scale(self):
        self.assertEqual(kde.format_scale(1.0), "1")
        self.assertEqual(kde.format_scale(1.5), "1.5")
        self.assertEqual(kde.format_scale(4 / 3), "1.333333")


class KscreenDoctorTest(unittest.TestCase):
    def result(self, stdout="", stderr="", returncode=0):
        return subprocess.CompletedProcess([], returncode, stdout, stderr)

    def test_query_outputs_runs_kscreen_doctor_json(self):
        text = fixture_text("kscreen-doctor-kwin682-virtual-placed.json")
        with mock.patch.object(kde.subprocess, "run",
                               return_value=self.result(text)) as run:
            outputs = kde.query_outputs()
        self.assertEqual(run.call_args.args[0], ["kscreen-doctor", "-j"])
        self.assertEqual(len(outputs), 2)

    def test_query_outputs_failures_give_an_empty_list(self):
        for effect in (OSError("missing"),
                       subprocess.TimeoutExpired("kscreen-doctor", 10)):
            with self.subTest(effect=effect), \
                    mock.patch.object(kde.subprocess, "run",
                                      side_effect=effect):
                self.assertEqual(kde.query_outputs(), [])
        with mock.patch.object(kde.subprocess, "run",
                               return_value=self.result("{}", returncode=1)):
            self.assertEqual(kde.query_outputs(), [])

    def test_rejections_raise_although_kscreen_doctor_exits_0(self):
        for message in ("applying config failed! Position of enabled output "
                        "Virtual-Tandem is negative (-5, 0)",
                        "Output with name or uuid Nope not found."):
            with self.subTest(message=message), \
                    mock.patch.object(kde.subprocess, "run",
                                      return_value=self.result(message)):
                with self.assertRaises(RuntimeError) as raised:
                    kde.run_kscreen_doctor(["output.Nope.position.-5,0"])
                self.assertIn(message.split("!")[0], str(raised.exception))

    def test_success_passes_every_argument_in_one_run(self):
        arguments = [f"output.{MAIN}.position.1600,0",
                     f"output.{TANDEM}.position.0,0"]
        with mock.patch.object(kde.subprocess, "run",
                               return_value=self.result()) as run:
            kde.run_kscreen_doctor(arguments)
        run.assert_called_once()
        self.assertEqual(run.call_args.args[0], ["kscreen-doctor", *arguments])

    def test_missing_kscreen_doctor_raises_runtime_error(self):
        with mock.patch.object(kde.subprocess, "run",
                               side_effect=FileNotFoundError("kscreen-doctor")):
            with self.assertRaises(RuntimeError):
                kde.run_kscreen_doctor(["output.X.scale.1"])


class PrimaryMonitorTest(unittest.TestCase):
    def test_primary_skips_our_own_outputs(self):
        outputs = [output("Virtual-Tandem-2", priority=1),
                   output("DP-1", x=1920), output("eDP-1", x=0)]
        self.assertEqual(kde.primary_output(outputs).name, "eDP-1")

    def test_describe_primary_monitor(self):
        outputs = fixture_outputs("kscreen-doctor-kwin675-virtual-placed.json")
        with mock.patch.object(kde, "query_outputs", return_value=outputs):
            text = kde.describe_primary_monitor()
        self.assertTrue(text.startswith(f"{MAIN}  1920x1080@"), text)
        self.assertTrue(text.endswith("scale 1"), text)

    def test_describe_primary_monitor_never_raises(self):
        with mock.patch.object(kde, "query_outputs",
                               side_effect=ValueError("boom")):
            self.assertIsNone(kde.describe_primary_monitor())
        with mock.patch.object(kde, "query_outputs", return_value=[]):
            self.assertIsNone(kde.describe_primary_monitor())


if __name__ == "__main__":
    unittest.main()
