from datetime import datetime, timezone
import unittest

from procrafiler.naming import MAX_STEM_CHARS, build_timestamped_filename, sanitize_filename_stem


class TestStemLengthCap(unittest.TestCase):
    """The stem can come from an AI. A model that answers the "name" field with a
    whole descriptive sentence would produce a filename the filesystem REFUSES
    (ENAMETOOLONG, 255 bytes on ext4 and most others), failing the placement of a
    document that is otherwise perfectly fine. Truncating beats refusing to file."""

    # A plausible runaway: a vision model describing the picture instead of titling it.
    VERBOSE = (
        "Photographie-d-un-document-administratif-relatif-a-un-sinistre-degat-des-eaux-"
        "survenu-dans-la-cuisine-du-logement-principal-avec-mention-de-l-expert-mandate-"
        "par-la-compagnie-d-assurance-et-les-references-completes-du-dossier-en-cours"
    )

    def test_a_runaway_stem_is_truncated(self) -> None:
        stem = sanitize_filename_stem(self.VERBOSE)
        self.assertLessEqual(len(stem.encode("utf-8")), MAX_STEM_CHARS)

    def test_a_short_stem_is_untouched(self) -> None:
        self.assertEqual(sanitize_filename_stem("Facture_EDF"), "Facture_EDF")

    def test_the_final_filename_always_fits_the_filesystem(self) -> None:
        """Prefix + stem + extension + a dedup suffix must stay under 255 bytes."""
        for length in (200, 400, 1000, 8000):
            with self.subTest(length=length):
                name = build_timestamped_filename("Description-tres-longue-" * length + ".jpg")
                # Leave room for a `__12`-style deduplication suffix on top.
                self.assertLess(len(name.encode("utf-8")) + 8, 255)

    def test_truncation_cuts_on_a_separator_when_one_is_near(self) -> None:
        """A chopped mid-word fragment reads badly; prefer a clean word boundary."""
        stem = sanitize_filename_stem(self.VERBOSE)
        self.assertFalse(stem.endswith(("-", "_")))
        self.assertTrue(stem)

    def test_a_truncated_stem_is_still_written_to_disk(self) -> None:
        import tempfile
        from pathlib import Path

        target_dir = Path(tempfile.mkdtemp())
        name = build_timestamped_filename(self.VERBOSE * 20 + ".pdf")
        (target_dir / name).write_bytes(b"x")  # must not raise ENAMETOOLONG
        self.assertTrue((target_dir / name).is_file())


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

    def test_existing_full_timestamp_prefix_is_not_doubled(self) -> None:
        # rescan ingesting a file the user already named in our format must not
        # double the prefix (run-17 produced …__00-00-00__…). The whole leading
        # YYYY-MM-DD_HH-MM-SS__ is dropped before the fresh prefix is applied.
        self.assertEqual(sanitize_filename_stem("2025-06-07_00-00-00__AR_CAF"), "AR_CAF")
        ts = datetime(2025, 6, 7, 0, 0, 0, tzinfo=timezone.utc)
        name = build_timestamped_filename("2025-06-07_00-00-00__AR_CAF.pdf", now_utc=ts)
        self.assertEqual(name, "2025-06-07_00-00-00__AR_CAF.pdf")


if __name__ == "__main__":
    unittest.main()
