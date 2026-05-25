"""Tests for the randomized daily-post timing logic."""

import random
import unittest

from moodful_responder import cli


class FakeRng:
    def __init__(self, value):
        self.value = value

    def random(self):
        return self.value


class WindowDecision(unittest.TestCase):
    def test_outside_window_never_posts(self):
        self.assertFalse(cli._should_post_this_hour(5, 6, 24, FakeRng(0.0)))
        self.assertFalse(cli._should_post_this_hour(24, 6, 24, FakeRng(0.0)))

    def test_final_hour_is_guaranteed(self):
        # hour 23, end 24 -> 1 slot left -> prob 1.0 regardless of the roll.
        self.assertTrue(cli._should_post_this_hour(23, 6, 24, FakeRng(0.999)))

    def test_probability_threshold(self):
        # hour 6, end 24 -> 18 slots -> threshold 1/18 ≈ 0.0556.
        self.assertTrue(cli._should_post_this_hour(6, 6, 24, FakeRng(0.01)))
        self.assertFalse(cli._should_post_this_hour(6, 6, 24, FakeRng(0.5)))

    def test_chosen_hour_is_uniform_over_the_window(self):
        # Simulate many days; each day walks the hours until one is chosen.
        rng = random.Random(0)
        counts = {h: 0 for h in range(6, 24)}
        days = 18000
        for _ in range(days):
            for h in range(6, 24):
                if cli._should_post_this_hour(h, 6, 24, rng):
                    counts[h] += 1
                    break
        self.assertEqual(sum(counts.values()), days)  # every day posts
        expected = days / 18  # 1000
        for h, c in counts.items():
            self.assertGreater(c, 800, f"hour {h} underrepresented: {c}")
            self.assertLess(c, 1200, f"hour {h} overrepresented: {c}")


class ParseWindow(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(cli._parse_window("6-24"), (6, 24))
        self.assertEqual(cli._parse_window("9-17"), (9, 17))

    def test_invalid(self):
        for bad in ("6", "24-6", "6-25", "a-b", ""):
            with self.assertRaises(ValueError):
                cli._parse_window(bad)


if __name__ == "__main__":
    unittest.main()
