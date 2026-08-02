# pyright: reportUnknownVariableType=false
"""Choosing which moments of a video are worth paying to look at.

Every still sent to the vision model is a billed call, so this is where the whole
video feature's cost lives. Three properties are load-bearing and each is pinned
here: the budget grows with duration but is capped, the two ends are always taken,
and nothing clusters.

The clustering rule is the one that was got wrong first: the best three moments of
a thirty-minute recording are routinely ten seconds apart in the same passage, and
a fixed two-second spacing floor happily accepted all three — paying three times
for one wall.
"""
from __future__ import annotations

import unittest

from procrafiler.frame_sampling import (
    Highlight,
    frame_budget,
    plan_frame_timestamps,
)


class BudgetTests(unittest.TestCase):
    def test_a_longer_video_earns_more_stills(self) -> None:
        budgets = [frame_budget(d) for d in (20, 90, 4 * 60, 10 * 60, 20 * 60, 60 * 60)]
        self.assertEqual(budgets, sorted(budgets), "the budget must never shrink with length")
        self.assertLess(budgets[0], budgets[-1], "…and must actually grow")

    def test_it_is_capped_so_a_long_recording_is_not_a_surprise_bill(self) -> None:
        self.assertEqual(frame_budget(60 * 60), frame_budget(6 * 60 * 60))
        self.assertLessEqual(frame_budget(24 * 60 * 60), 12)

    def test_a_zero_length_video_earns_nothing(self) -> None:
        self.assertEqual(frame_budget(0), 0)
        self.assertEqual(frame_budget(-5), 0)


class PlanTests(unittest.TestCase):
    def test_both_ends_are_always_taken(self) -> None:
        duration = 300.0
        plan = plan_frame_timestamps(duration)
        self.assertLess(plan[0], duration * 0.1, "a still from the opening")
        self.assertGreater(plan[-1], duration * 0.9, "and one from the end")

    def test_neither_end_sits_exactly_on_the_boundary(self) -> None:
        """0.0 is often a black frame or a fade, and the very last frame is not
        reliably seekable across containers — both would waste a paid call."""
        duration = 300.0
        plan = plan_frame_timestamps(duration)
        self.assertGreater(plan[0], 0.0)
        self.assertLess(plan[-1], duration)

    def test_the_budget_is_respected_exactly(self) -> None:
        for duration in (10, 60, 600, 3600):
            with self.subTest(duration=duration):
                self.assertLessEqual(len(plan_frame_timestamps(duration)), frame_budget(duration))

    def test_clustered_highlights_do_not_buy_the_same_shot_three_times(self) -> None:
        """The failure this rule exists for: a model ranks three moments inside one
        passage, and a naive sampler pays for all three."""
        clustered = [Highlight(600, 610), Highlight(612, 620), Highlight(615, 625)]
        plan = plan_frame_timestamps(1800, clustered)
        near_the_cluster = [t for t in plan if 590 <= t <= 640]
        self.assertEqual(len(near_the_cluster), 1, f"one still from that passage, got {near_the_cluster}")

    def test_distinct_highlights_are_all_honoured(self) -> None:
        spread = [Highlight(300, 310), Highlight(900, 910), Highlight(1500, 1510)]
        plan = plan_frame_timestamps(1800, spread)
        for wanted in (305.0, 905.0, 1505.0):
            with self.subTest(wanted=wanted):
                self.assertTrue(
                    any(abs(t - wanted) < 1.0 for t in plan), f"{wanted} missing from {plan}"
                )

    def test_highlights_are_preferred_in_the_order_given(self) -> None:
        """The model ranks them; a tight budget must spend on its first choice."""
        ranked = [Highlight(1000, 1010), Highlight(200, 210)]
        plan = plan_frame_timestamps(1800, ranked, budget=3)
        self.assertTrue(any(abs(t - 1005.0) < 1.0 for t in plan), f"top choice dropped: {plan}")

    def test_no_transcript_falls_back_to_an_even_spread(self) -> None:
        """A silent video still has to be looked at — evenly, since nothing says
        where to look."""
        plan = plan_frame_timestamps(600)
        self.assertEqual(len(plan), frame_budget(600))
        gaps = [b - a for a, b in zip(plan, plan[1:])]
        self.assertLess(max(gaps) - min(gaps), max(gaps) * 0.9, f"not an even spread: {plan}")

    def test_a_very_short_clip_still_gets_several_stills(self) -> None:
        """The spacing rule must scale down, not reject everything and return one
        frame for a four-second video."""
        plan = plan_frame_timestamps(4)
        self.assertGreaterEqual(len(plan), 3)
        self.assertEqual(sorted(plan), plan)

    def test_every_timestamp_is_inside_the_video(self) -> None:
        for duration in (3, 47, 601, 3607):
            with self.subTest(duration=duration):
                for at in plan_frame_timestamps(duration, [Highlight(duration * 0.5)]):
                    self.assertGreaterEqual(at, 0.0)
                    self.assertLessEqual(at, duration)

    def test_a_highlight_outside_the_video_is_ignored(self) -> None:
        """A model that invents a timestamp past the end would send ffmpeg seeking
        into nothing, and buy us a black frame."""
        plan = plan_frame_timestamps(100, [Highlight(99999), Highlight(-40)])
        self.assertTrue(all(0 <= t <= 100 for t in plan), plan)

    def test_zero_duration_plans_nothing(self) -> None:
        self.assertEqual(plan_frame_timestamps(0), [])
        self.assertEqual(plan_frame_timestamps(-1), [])

    def test_the_plan_is_deterministic(self) -> None:
        highlights = [Highlight(120, 130), Highlight(400, 410)]
        first = plan_frame_timestamps(900, highlights)
        self.assertEqual(first, plan_frame_timestamps(900, highlights))

    def test_a_highlight_is_sampled_mid_passage_not_at_its_first_word(self) -> None:
        """Someone starts describing a thing slightly before showing it."""
        plan = plan_frame_timestamps(600, [Highlight(100, 140)], budget=3)
        self.assertTrue(any(abs(t - 120.0) < 1.0 for t in plan), plan)


if __name__ == "__main__":
    unittest.main()
