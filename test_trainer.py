from datetime import date
import unittest

from judge import _parse_prediction
from trainer import (ReplaySetup, build_playbook, split_lessons,
                     update_prediction_stats, update_stats)


def make_setup(r: float, strategy: str = "opening_scalp",
               direction: str = "long", regime: str = "chop",
               bucket: str = "open_0930_1000") -> ReplaySetup:
    return ReplaySetup(
        day=date(2026, 6, 1), ticker="AAPL", strategy=strategy,
        direction=direction, entry=100.0, stop=99.5, target=101.0,
        r_multiple=r, regime=regime, bucket=bucket, reasoning={})


class StatsTests(unittest.TestCase):
    def test_bucket_aggregation(self):
        stats = {}
        update_stats(stats, make_setup(1.5))
        update_stats(stats, make_setup(-1.0))
        entry = stats["buckets"]["opening_scalp|long|chop|open_0930_1000"]
        self.assertEqual(entry["n"], 2)
        self.assertEqual(entry["wins"], 1)
        self.assertAlmostEqual(entry["sum_r"], 0.5)

    def test_prediction_accuracy_tracked_per_strategy_and_total(self):
        stats = {}
        update_prediction_stats(stats, "ob_fvg", True)
        update_prediction_stats(stats, "ob_fvg", False)
        update_prediction_stats(stats, "support_hold", True)
        self.assertEqual(stats["predictions"]["ob_fvg"], {"n": 2, "correct": 1})
        self.assertEqual(stats["predictions"]["_total"], {"n": 3, "correct": 2})


class PlaybookTests(unittest.TestCase):
    cfg = {"min_bucket_n": 3, "warn_expectancy_r": -0.05,
           "edge_expectancy_r": 0.20}

    def test_small_samples_never_qualify(self):
        stats = {}
        update_stats(stats, make_setup(-1.0))
        update_stats(stats, make_setup(-1.0))
        text = build_playbook(stats, self.cfg)
        self.assertNotIn("AVOID", text)
        self.assertIn("keep training", text)

    def test_negative_expectancy_becomes_avoid_line(self):
        stats = {}
        for r in (-1.0, -1.0, 0.4):
            update_stats(stats, make_setup(r, direction="short",
                                           regime="trend_up"))
        text = build_playbook(stats, self.cfg)
        self.assertIn("## OPENING_SCALP", text)
        self.assertIn("- AVOID: short in trend_up during open_0930_1000", text)
        self.assertIn("3 replays", text)

    def test_positive_expectancy_becomes_edge_line(self):
        stats = {}
        for r in (1.5, 1.5, -1.0):
            update_stats(stats, make_setup(r, strategy="support_hold",
                                           regime="trend_up"))
        text = build_playbook(stats, self.cfg)
        self.assertIn("## SUPPORT_HOLD", text)
        self.assertIn("- EDGE: long in trend_up during open_0930_1000", text)

    def test_flat_expectancy_stays_silent(self):
        stats = {}
        for r in (0.1, 0.0, -0.1):
            update_stats(stats, make_setup(r))
        text = build_playbook(stats, self.cfg)
        self.assertNotIn("AVOID", text)
        self.assertNotIn("EDGE", text)


class LessonSplitTests(unittest.TestCase):
    def test_splits_markdown_into_entries(self):
        text = ("## A — WIN\n- Setup: x\n- Keep in mind: y\n\n"
                "## B — LOSS\n- Setup: z\n")
        entries = split_lessons(text)
        self.assertEqual(len(entries), 2)
        self.assertTrue(entries[0].startswith("## A"))
        self.assertTrue(entries[1].startswith("## B"))
        self.assertIn("Keep in mind: y", entries[0])

    def test_empty_file_yields_no_entries(self):
        self.assertEqual(split_lessons(""), [])
        self.assertEqual(split_lessons("\n\n"), [])


class PredictionParseTests(unittest.TestCase):
    def test_parses_well_formed_answer(self):
        prediction, reason = _parse_prediction(
            "PREDICTION: WIN\nREASON: regime favors longs")
        self.assertEqual(prediction, "WIN")
        self.assertEqual(reason, "regime favors longs")

    def test_rejects_malformed_answer(self):
        prediction, reason = _parse_prediction("I think it goes up")
        self.assertIsNone(prediction)
        self.assertIn("not in required format", reason)


if __name__ == "__main__":
    unittest.main()
