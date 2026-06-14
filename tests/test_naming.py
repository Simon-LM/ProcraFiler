from datetime import datetime, timezone
import unittest

from procrafiler.naming import build_timestamped_filename, sanitize_filename_stem


class TestNaming(unittest.TestCase):
    def test_utc_prefixed_filename(self) -> None:
        ts = datetime(2026, 4, 1, 22, 10, 6, tzinfo=timezone.utc)
        name = build_timestamped_filename("Tax Report 2024.pdf", now_utc=ts)
        self.assertEqual(name, "2026-04-01_22-10-06__Tax-Report-2024.pdf")

    def test_stem_preserves_case_and_underscore(self) -> None:
        # The CV template is CV_LOUVEL-Simon: the underscore and the uppercase
        # surname must survive sanitization.
        self.assertEqual(sanitize_filename_stem("CV_LOUVEL-Simon"), "CV_LOUVEL-Simon")

    def test_stem_never_contains_double_underscore(self) -> None:
        # `__` is reserved as the timestamp-prefix separator: any separator run
        # containing an underscore collapses to one underscore.
        self.assertEqual(sanitize_filename_stem("CV__LOUVEL"), "CV_LOUVEL")
        self.assertEqual(sanitize_filename_stem("CV _ LOUVEL"), "CV_LOUVEL")
        self.assertEqual(sanitize_filename_stem("_CV LOUVEL_"), "CV-LOUVEL")

    def test_stem_strips_redundant_leading_date(self) -> None:
        # Run 6: the AI sometimes leaks a date into the stem (…__2025-08_Degats…)
        # though the timestamp prefix already carries it. A month-precision date
        # that LEADS the stem is dropped deterministically.
        self.assertEqual(sanitize_filename_stem("2025-08_Degats-eaux-cuisine"), "Degats-eaux-cuisine")
        self.assertEqual(sanitize_filename_stem("2025-08-15_Facture-EDF"), "Facture-EDF")

    def test_stem_keeps_bare_year_in_identity(self) -> None:
        # A bare year can be part of the identity (the census of 2026); only a
        # leading month-precision date is stripped, never a bare year.
        self.assertEqual(sanitize_filename_stem("Recensement-population_2026"), "Recensement-population_2026")
        self.assertEqual(sanitize_filename_stem("2026_Budget"), "2026_Budget")

    def test_timestamped_filename_strips_leading_date_in_stem(self) -> None:
        ts = datetime(2026, 4, 1, 22, 10, 6, tzinfo=timezone.utc)
        name = build_timestamped_filename("2025-08_Degats-eaux-cuisine.jpg", now_utc=ts)
        self.assertEqual(name, "2026-04-01_22-10-06__Degats-eaux-cuisine.jpg")


if __name__ == "__main__":
    unittest.main()
