from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.user_context_setup import collect_answers, render_context, setup_context


def _scripted(answers: list[str]):
    """An `ask` that returns the scripted answers in order, ignoring the prompt."""
    it = iter(answers)
    return lambda _prompt: next(it)


def _sink() -> tuple[list[str], "object"]:
    lines: list[str] = []
    return lines, lines.append


class TestCollectAnswers(unittest.TestCase):
    def test_self_employed_path_asks_business_and_work_names(self) -> None:
        # status "2" = self-employed → asks profession, business, work names (NOT employer).
        ask = _scripted(
            ["Alex", "Martin", "handle", "2", "Dev", "MyBiz", "ClientX, ProjY",
             "1", "", "", "", "", "", "", "", "a note"]
        )
        _, out = _sink()
        a = collect_answers(ask, out)
        self.assertEqual(a["work_status"], "self")
        self.assertEqual(a["business"], "MyBiz")
        self.assertEqual(a["work_names"], ["ClientX", "ProjY"])
        self.assertEqual(a["interests"], ["Musique"])  # "1" → first option
        self.assertNotIn("employer", a)

    def test_no_activity_path_skips_all_work_questions(self) -> None:
        ask = _scripted(["Bo", "Lee", "", "4", "", "", "", "", "", "", "", "", ""])
        _, out = _sink()
        a = collect_answers(ask, out)
        self.assertEqual(a["work_status"], "none")
        for skipped in ("profession", "employer", "business", "work_names"):
            self.assertNotIn(skipped, a)

    def test_checklist_mixes_numbers_and_free_text(self) -> None:
        ask = _scripted(["Bo", "Lee", "", "4", "1, 3, voile", "", "", "", "", "", "", "", ""])
        _, out = _sink()
        a = collect_answers(ask, out)
        self.assertEqual(a["interests"], ["Musique", "Lecture", "voile"])


class TestRenderContext(unittest.TestCase):
    def test_renders_only_filled_fields(self) -> None:
        text = render_context(
            {
                "first_name": "Alex", "last_name": "Martin",
                "work_status": "self", "business": "MyBiz",
                "work_names": ["ClientX", "ProjY"],
                "interests": ["Musique", "Voile"],
            }
        )
        self.assertIn("My name is Alex Martin.", text)
        self.assertIn("My business: MyBiz.", text)
        self.assertIn("Names that mean my work: ClientX, ProjY.", text)
        self.assertIn("My hobbies / interests: Musique, Voile.", text)
        # No household section when nothing was filled.
        self.assertNotIn("[Household]", text)

    def test_empty_answers_render_minimal(self) -> None:
        text = render_context({"work_status": "none"})
        self.assertIn("no professional activity", text)
        self.assertNotIn("[About me]", text)


class TestSetupContext(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.pop("PROCRAFILER_CONTEXT_FILE", None)
        os.environ["PROCRAFILER_CONFIG_HOME"] = self.tmp.name

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_CONFIG_HOME", None)
        self.tmp.cleanup()

    def test_save_writes_the_context_file(self) -> None:
        ask = _scripted(
            ["Alex", "Martin", "", "4", "2", "", "", "", "", "", "", "", "", "1"]  # last "1" = Enregistrer
        )
        _, out = _sink()
        target = setup_context(ask=ask, out=out)
        self.assertIsNotNone(target)
        assert target is not None
        self.assertEqual(target, Path(self.tmp.name) / "context.md")
        self.assertTrue(target.is_file())
        body = target.read_text(encoding="utf-8")
        self.assertIn("Alex Martin", body)
        self.assertIn("Sport", body)  # interests "2"

    def test_cancel_writes_nothing(self) -> None:
        ask = _scripted(
            ["Alex", "Martin", "", "4", "", "", "", "", "", "", "", "", "", "2"]  # last "2" = Annuler
        )
        _, out = _sink()
        target = setup_context(ask=ask, out=out)
        self.assertIsNone(target)
        self.assertFalse((Path(self.tmp.name) / "context.md").exists())


if __name__ == "__main__":
    unittest.main()
