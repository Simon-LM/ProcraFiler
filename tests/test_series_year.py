from __future__ import annotations

import json
import unittest

from procrafiler.pipeline import _fiche_year, _with_series_year
from procrafiler.taxonomy import base_category_for


class TestBaseCategoryFor(unittest.TestCase):
    def test_returns_longest_matching_base(self) -> None:
        self.assertEqual(
            base_category_for(("Personal", "Administrative", "Utilities", "EDF")),
            ("Personal", "Administrative", "Utilities"),
        )
        self.assertEqual(
            base_category_for(("Work", "Employment", "CV")),
            ("Work", "Employment"),
        )

    def test_a_base_itself_matches_itself(self) -> None:
        self.assertEqual(base_category_for(("Personal", "Education")), ("Personal", "Education"))

    def test_manual_review_has_no_base(self) -> None:
        self.assertIsNone(base_category_for(("Manual_Review",)))


class TestWithSeriesYear(unittest.TestCase):
    UTIL = ("Personal", "Administrative", "Utilities")

    def test_appends_year_to_entity_folder(self) -> None:
        self.assertEqual(
            _with_series_year((*self.UTIL, "EDF"), series=True, year="2026"),
            (*self.UTIL, "EDF", "2026"),
        )

    def test_no_op_when_not_a_series(self) -> None:
        route = (*self.UTIL, "EDF")
        self.assertEqual(_with_series_year(route, series=False, year="2026"), route)

    def test_no_op_at_a_bare_base(self) -> None:
        # A cert that landed flat in a base (no entity folder) is not dated.
        route = ("Personal", "Education")
        self.assertEqual(_with_series_year(route, series=True, year="2023"), route)

    def test_no_op_when_already_dated(self) -> None:
        route = (*self.UTIL, "EDF", "2026")
        self.assertEqual(_with_series_year(route, series=True, year="2026"), route)

    def test_no_op_without_a_usable_year(self) -> None:
        route = (*self.UTIL, "EDF")
        self.assertEqual(_with_series_year(route, series=True, year=None), route)
        self.assertEqual(_with_series_year(route, series=True, year="20xx"), route)

    def test_no_op_under_no_base(self) -> None:
        route = ("Manual_Review",)
        self.assertEqual(_with_series_year(route, series=True, year="2026"), route)


class TestFicheYear(unittest.TestCase):
    def test_prefers_document_date(self) -> None:
        fiche = json.dumps({"document_date": "2024-05-07", "effective_date": "2026-04-02"})
        self.assertEqual(_fiche_year(fiche), "2024")

    def test_falls_back_to_effective_date(self) -> None:
        fiche = json.dumps({"document_date": None, "effective_date": "2025-10-13"})
        self.assertEqual(_fiche_year(fiche), "2025")

    def test_none_or_garbage_returns_none(self) -> None:
        self.assertIsNone(_fiche_year(None))
        self.assertIsNone(_fiche_year("not json"))
        self.assertIsNone(_fiche_year(json.dumps({"document_date": "bad"})))


if __name__ == "__main__":
    unittest.main()
