"""Tests for backends/layout.py: where the phone's monitor goes."""

import support  # noqa: F401  (must come first: sys.path + scratch HOME)

import unittest

from backends import gnome, hyprland, kde, layout

LAPTOP = (0, 0, 1920, 1080)
PHONE = (2400, 1080)


class SidePhraseAndScaleTest(unittest.TestCase):
    def test_side_phrases_read_as_english(self):
        self.assertEqual([layout.side_phrase(side)
                          for side in ("left", "right", "up", "down")],
                         ["left of", "right of", "above", "below"])

    def test_valid_scale_is_shared(self):
        # One implementation, still reachable under its old name.
        self.assertIs(hyprland.valid_scale, layout.valid_scale)
        self.assertIs(kde.valid_scale, layout.valid_scale)
        self.assertAlmostEqual(layout.valid_scale(2772, 1280, 1.5), 4 / 3,
                               places=5)
        self.assertEqual(layout.valid_scale(2400, 1080, 1.5), 1.5)


class PlaceBesideTest(unittest.TestCase):
    def test_flush_against_a_lone_monitor_on_every_side(self):
        expected = {"right": (1920, 0), "left": (-2400, 0),
                    "up": (0, -1080), "down": (0, 1080)}
        for side, spot in expected.items():
            with self.subTest(side=side):
                self.assertEqual(
                    layout.place_beside(LAPTOP, [LAPTOP], PHONE, side), spot)

    def test_unknown_side_means_right(self):
        self.assertEqual(layout.place_beside(LAPTOP, [LAPTOP], PHONE, "?"),
                         (1920, 0))

    def test_occupied_spot_moves_to_the_outer_edge(self):
        # A monitor already sits right of the laptop: go right of that one,
        # lined up with it (it forms the edge).
        external = (1920, 0, 2560, 1440)
        self.assertEqual(
            layout.place_beside(LAPTOP, [LAPTOP, external], PHONE, "right"),
            (4480, 0))

    def test_outer_edge_lines_up_with_the_monitor_nearest_the_anchor(self):
        anchor = (0, 0, 1920, 1080)
        above = (0, -1440, 2560, 1440)
        far_above = (2560, -1440, 1920, 1080)   # same top edge, further away
        self.assertEqual(
            layout.place_beside(anchor, [anchor, above, far_above], PHONE, "up"),
            (0, -2520))

    def test_left_of_a_layout_that_extends_left(self):
        anchor = (0, 0, 1920, 1080)
        left = (-2560, 200, 2560, 1440)
        self.assertEqual(
            layout.place_beside(anchor, [anchor, left], PHONE, "left"),
            (-4960, 200))

    def test_down_below_a_monitor_in_the_way(self):
        anchor = (0, 0, 1920, 1080)
        below = (0, 1080, 1920, 1080)
        self.assertEqual(
            layout.place_beside(anchor, [anchor, below], PHONE, "down"),
            (0, 2160))

    def test_result_never_overlaps(self):
        anchor = (0, 0, 1920, 1080)
        others = [anchor, (1920, 0, 1280, 1024), (-1280, 0, 1280, 1024),
                  (0, 1080, 1920, 1080), (0, -900, 1600, 900)]
        for side in ("right", "left", "up", "down"):
            with self.subTest(side=side):
                x, y = layout.place_beside(anchor, others, PHONE, side)
                for rect in others:
                    self.assertFalse(layout._overlaps((x, y, *PHONE), rect))


class OverlapsTest(unittest.TestCase):
    def test_touching_edges_do_not_overlap(self):
        self.assertFalse(layout._overlaps((0, 0, 10, 10), (10, 0, 10, 10)))
        self.assertFalse(layout._overlaps((0, 0, 10, 10), (0, 10, 10, 10)))

    def test_shared_area_overlaps(self):
        self.assertTrue(layout._overlaps((0, 0, 10, 10), (9, 9, 10, 10)))
        self.assertTrue(layout._overlaps((0, 0, 10, 10), (2, 2, 1, 1)))


class ReExportTest(unittest.TestCase):
    def test_gnome_reexports_the_layout_helpers(self):
        self.assertIs(gnome.place_beside, layout.place_beside)
        self.assertIs(gnome._overlaps, layout._overlaps)


if __name__ == "__main__":
    unittest.main()
