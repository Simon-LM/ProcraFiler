# pyright: reportUnknownVariableType=false
"""Two things a user needs before letting the app loose on real documents: knowing
what a run will cost, and being able to try it on a handful of files first.

Neither existed. `process-all` had one flag (`--dry-run`), so "start small" meant
physically moving files out of the Inbox and back; and nothing said how many paid
AI calls a batch would make — the only warning was "this may take a while and use
AI" past a threshold, with no number.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from procrafiler.ai_estimate import AICallEstimate, estimate_ai_calls, format_estimate
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import _build_work_sets, _limit_work_sets, process_all_inbox_files

ALL_CHAINS = {
    "PROCRAFILER_AI_IMAGE_PRIMARY": "mistral:img",
    "PROCRAFILER_AI_OCR_PRIMARY": "mistral:ocr",
    "PROCRAFILER_AI_ANALYSIS_PRIMARY": "mistral:ana",
    "PROCRAFILER_AI_NAMING_PRIMARY": "mistral:nam",
    "PROCRAFILER_AI_ORGANIZE_PRIMARY": "mistral:org",
}
CHAIN_VARS = tuple(ALL_CHAINS)


def _sets(*groups: tuple[str, list[str]]) -> list[tuple[str, list[Path]]]:
    return [(top, [Path(n) for n in names]) for top, names in groups]


class _Chains(unittest.TestCase):
    """Every chain configured unless a test says otherwise."""

    def setUp(self) -> None:
        self._saved = {k: os.environ.get(k) for k in CHAIN_VARS}
        os.environ.update(ALL_CHAINS)

    def tearDown(self) -> None:
        for key, value in self._saved.items():
            os.environ.pop(key, None)
            if value is not None:
                os.environ[key] = value


class TestTheEstimate(_Chains):
    def test_each_media_type_is_charged_for_what_it_actually_costs(self) -> None:
        estimate = estimate_ai_calls(
            _sets(("Claim", ["a.jpg", "b.jpg", "scan.pdf", "notes.txt", "movie.mkv"]))
        )
        self.assertEqual(estimate.vision_reads, 2)
        self.assertEqual(estimate.maybe_ocr_reads, 1)
        self.assertEqual(estimate.local_reads, 1)   # .txt is read on disk, free
        self.assertEqual(estimate.unreadable, 1)    # no video reader → manual review
        self.assertEqual(estimate.analyses, 4, "an unreadable file must not be charged")

    def test_naming_and_organize_are_charged_once_per_folder_never_per_file(self) -> None:
        """They see the whole set at once — that is the entire point of them."""
        estimate = estimate_ai_calls(
            _sets(("Claim", ["a.jpg", "b.jpg", "c.jpg"]), ("Trip", ["d.jpg"]))
        )
        self.assertEqual(estimate.naming_passes, 2)
        self.assertEqual(estimate.organize_passes, 2)

    def test_a_loose_file_is_not_a_set_and_pays_for_neither(self) -> None:
        estimate = estimate_ai_calls(_sets(("", ["alone.jpg"])))
        self.assertEqual(estimate.naming_passes, 0)
        self.assertEqual(estimate.organize_passes, 0)

    def test_the_range_ends_are_the_two_honest_extremes(self) -> None:
        """A PDF may or may not be a scan; a photo may or may not be a document.
        Neither can be known without opening the file, so the answer is a range."""
        estimate = estimate_ai_calls(_sets(("Claim", ["a.jpg", "scan.pdf"])))
        # min: the PDF has a text layer, the photo is a scene.
        self.assertEqual(estimate.minimum, 1 + 2 + 1 + 1)  # vision + analyses + naming + organize
        # max: the PDF is a scan (+1) and the photo is a document (+1 OCR confirm).
        self.assertEqual(estimate.maximum, estimate.minimum + 2)

    def test_a_task_with_no_chain_configured_is_not_charged(self) -> None:
        for var in CHAIN_VARS:
            os.environ.pop(var, None)
        estimate = estimate_ai_calls(_sets(("Claim", ["a.jpg", "scan.pdf", "n.txt"])))
        self.assertEqual((estimate.minimum, estimate.maximum), (0, 0))

    def test_vision_without_ocr_pays_for_the_read_and_never_the_confirmation(self) -> None:
        """The gating is per task, not global: the OCR re-read of a photographed
        document simply cannot happen without an OCR chain."""
        os.environ.pop("PROCRAFILER_AI_OCR_PRIMARY", None)
        estimate = estimate_ai_calls(_sets(("Claim", ["a.jpg", "scan.pdf"])))
        self.assertEqual(estimate.vision_reads, 1)
        self.assertEqual(estimate.maybe_ocr_reads, 0, "a PDF cannot be OCR'd with no OCR chain")
        self.assertEqual(estimate.minimum, estimate.maximum, "nothing is uncertain any more")

    def test_the_text_names_the_number_and_admits_what_it_cannot_know(self) -> None:
        text = format_estimate(estimate_ai_calls(_sets(("Claim", ["a.jpg", "scan.pdf"]))))
        self.assertIn("AI call", text)
        self.assertIn(" to ", text, "a range must read as a range")
        self.assertIn("Duplicates", text, "the known imprecision is not stated")

    def test_nothing_configured_says_so_plainly(self) -> None:
        text = format_estimate(AICallEstimate(files=3))
        self.assertIn("no AI call", text)

    def test_an_empty_inbox_says_nothing_to_do(self) -> None:
        self.assertIn("Nothing", format_estimate(estimate_ai_calls([])))


class TestTheLimitNeverSplitsASet(unittest.TestCase):
    def test_whole_sets_are_kept_up_to_the_limit(self) -> None:
        sets = _sets(("A", ["1", "2"]), ("B", ["3", "4"]), ("C", ["5"]))
        kept, deferred_sets, deferred_files = _limit_work_sets(sets, 3)
        self.assertEqual([top for top, _ in kept], ["A"])
        self.assertEqual((deferred_sets, deferred_files), (2, 3))

    def test_a_folder_bigger_than_the_limit_is_still_processed_whole(self) -> None:
        """Otherwise `--limit 1` on a ten-file folder makes no progress, ever — and
        half a folder would be named against half its context, which is the exact
        misreading the set pass exists to prevent."""
        sets = _sets(("Big", [str(i) for i in range(10)]), ("Next", ["x"]))
        kept, _deferred_sets, deferred_files = _limit_work_sets(sets, 1)
        self.assertEqual(len(kept), 1)
        self.assertEqual(len(kept[0][1]), 10, "the set was split")
        self.assertEqual(deferred_files, 1)

    def test_a_limit_above_the_total_defers_nothing(self) -> None:
        sets = _sets(("A", ["1"]), ("B", ["2"]))
        kept, deferred_sets, deferred_files = _limit_work_sets(sets, 99)
        self.assertEqual(kept, sets)
        self.assertEqual((deferred_sets, deferred_files), (0, 0))


class TestLimitInThePipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _drop(self, relative: str) -> None:
        target = self.paths.inbox_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(f"content of {relative}", encoding="utf-8")

    def test_the_rest_stays_in_the_inbox_for_the_next_run(self) -> None:
        self._drop("Claim/a.txt")
        self._drop("Claim/b.txt")
        self._drop("Trip/c.txt")
        self._drop("loose.txt")

        summary = process_all_inbox_files(self.paths, now_utc=self.now, limit=2)

        self.assertEqual(summary["total"], 2, "the limit was not applied")
        left = sorted(p.name for p in self.paths.inbox_dir.rglob("*") if p.is_file())
        self.assertEqual(left, ["c.txt", "loose.txt"], "the wrong files were left behind")

        # …and a second run finishes the job, with nothing lost in between.
        process_all_inbox_files(self.paths, now_utc=self.now, limit=2)
        self.assertEqual([p for p in self.paths.inbox_dir.rglob("*") if p.is_file()], [])

    def test_without_a_limit_everything_is_processed(self) -> None:
        """Anti-vacuity: the test above must fail because of the LIMIT."""
        self._drop("Claim/a.txt")
        self._drop("Claim/b.txt")
        self._drop("Trip/c.txt")

        summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["total"], 3)
        self.assertEqual([p for p in self.paths.inbox_dir.rglob("*") if p.is_file()], [])

    def test_the_cost_is_announced_before_the_run_starts(self) -> None:
        self._drop("Claim/a.txt")
        messages: list[str] = []

        process_all_inbox_files(
            self.paths, now_utc=self.now, progress=messages.append
        )

        estimate_lines = [m for m in messages if "AI call" in m or "no AI call" in m]
        self.assertTrue(estimate_lines, f"no cost estimate was emitted: {messages}")

    def test_a_dry_run_also_honours_the_limit(self) -> None:
        """A dry run whose numbers ignored `--limit` would preview the wrong run."""
        self._drop("Claim/a.txt")
        self._drop("Claim/b.txt")
        self._drop("Trip/c.txt")

        summary = process_all_inbox_files(self.paths, now_utc=self.now, dry_run=True, limit=2)

        self.assertEqual(summary["total"], 2)
        # And it really was a dry run.
        self.assertEqual([p for p in self.paths.library_root.rglob("*") if p.is_file()], [])


class TestWorkSetGrouping(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_nested_folder_belongs_to_its_top_level_set(self) -> None:
        """`Claim/photos/a.jpg` and `Claim/b.pdf` are one drop, not two."""
        for relative in ("Claim/photos/a.txt", "Claim/b.txt", "loose.txt"):
            target = self.paths.inbox_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")

        work_sets = _build_work_sets(self.paths)

        by_top = {top: len(members) for top, members in work_sets}
        self.assertEqual(by_top, {"Claim": 2, "": 1})

    def test_the_order_is_deterministic_so_the_limit_is_predictable(self) -> None:
        for relative in ("B/x.txt", "A/y.txt", "z.txt"):
            target = self.paths.inbox_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text("x", encoding="utf-8")

        first = [top for top, _ in _build_work_sets(self.paths)]
        second = [top for top, _ in _build_work_sets(self.paths)]
        self.assertEqual(first, second)
        self.assertEqual(first[:2], ["A", "B"], "folder sets are not in a stable order")


if __name__ == "__main__":
    unittest.main()
