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


def _sink() -> tuple[list[str], object]:
    lines: list[str] = []
    return lines, lines.append


# The questionnaire is linear (no branching); 17 questions in order.
def _answers(
    *,
    first="", last="", aliases="", professions="", employers="", businesses="",
    work_names="", interests="", banks="", insurers="", energy="", telecom="",
    rentals="", properties="", vehicles="", household="", notes="",
) -> list[str]:
    return [
        first, last, aliases, professions, employers, businesses, work_names,
        interests, banks, insurers, energy, telecom, rentals, properties,
        vehicles, household, notes,
    ]


class TestCollectAnswers(unittest.TestCase):
    def test_multi_value_fields_split_on_commas(self) -> None:
        # A filing tool sees OLD documents → most fields are multi-value
        # (current AND past). Commas separate them (names can contain spaces).
        ask = _scripted(_answers(
            first="Alex", last="Martin", aliases="alexm, am",
            professions="Dev, Prof", employers="Acme, Globex", businesses="MyBiz",
            work_names="ClientX, ProjY", interests="1, 3, voile",
            banks="BNP Paribas, Crédit Agricole", telecom="Free, Orange",
            rentals="Annoville, Fougères", properties="Paris 11e", household="Sam, Lou",
        ))
        _, out = _sink()
        a = collect_answers(ask, out)
        self.assertEqual(a["professions"], ["Dev", "Prof"])
        self.assertEqual(a["employers"], ["Acme", "Globex"])
        self.assertEqual(a["work_names"], ["ClientX", "ProjY"])
        self.assertEqual(a["interests"], ["Musique", "Lecture", "voile"])  # 1,3 + free
        self.assertEqual(a["banks"], ["BNP Paribas", "Crédit Agricole"])   # spaces kept
        self.assertEqual(a["telecom"], ["Free", "Orange"])
        self.assertEqual(a["rentals"], ["Annoville", "Fougères"])          # place labels
        self.assertEqual(a["properties"], ["Paris 11e"])
        self.assertEqual(a["household"], ["Sam", "Lou"])

    def test_everything_skippable(self) -> None:
        _, out = _sink()
        a = collect_answers(_scripted(_answers()), out)
        self.assertEqual(a["professions"], [])
        self.assertEqual(a["interests"], [])
        self.assertEqual(a["notes"], "")


class TestRenderContext(unittest.TestCase):
    def test_renders_history_fields(self) -> None:
        text = render_context({
            "first_name": "Alex", "last_name": "Martin",
            "professions": ["Dev", "Prof"], "employers": ["Acme", "Globex"],
            "work_names": ["ClientX"], "interests": ["Musique"],
            "banks": ["BNP Paribas", "Crédit Agricole"],
            "rentals": ["Annoville", "Fougères"], "properties": ["Paris 11e"],
        })
        self.assertIn("My name is Alex Martin.", text)
        self.assertIn("Professions (current or past): Dev, Prof.", text)
        self.assertIn("Employers (current or past): Acme, Globex.", text)
        self.assertIn("Names that mean my work: ClientX.", text)
        self.assertIn("Banks (current or past): BNP Paribas, Crédit Agricole.", text)
        self.assertIn("Rented homes (current or past), by place: Annoville, Fougères.", text)
        self.assertIn("Owned properties (current or past), by place: Paris 11e.", text)

    def test_empty_renders_nothing(self) -> None:
        self.assertEqual(render_context({}).strip(), "")


class TestSetupContext(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.pop("PROCRAFILER_CONTEXT_FILE", None)
        os.environ["PROCRAFILER_CONFIG_HOME"] = self.tmp.name

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_CONFIG_HOME", None)
        self.tmp.cleanup()

    def test_save_writes_the_context_file(self) -> None:
        ask = _scripted(_answers(first="Alex", last="Martin", professions="Dev") + ["1"])  # "1" = Enregistrer
        _, out = _sink()
        target = setup_context(ask=ask, out=out)
        self.assertEqual(target, Path(self.tmp.name) / "context.md")
        assert target is not None
        body = target.read_text(encoding="utf-8")
        self.assertIn("Alex Martin", body)
        self.assertIn("Dev", body)

    def test_cancel_writes_nothing(self) -> None:
        ask = _scripted(_answers(first="Alex", last="Martin") + ["2"])  # "2" = Annuler
        _, out = _sink()
        self.assertIsNone(setup_context(ask=ask, out=out))
        self.assertFalse((Path(self.tmp.name) / "context.md").exists())


if __name__ == "__main__":
    unittest.main()
