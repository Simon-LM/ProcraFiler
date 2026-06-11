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


if __name__ == "__main__":
    unittest.main()
