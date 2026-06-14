# pyright: reportUnknownVariableType=false
from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_all_inbox_files

AFFAIR = "Personal/Administrative/Insurance/Degats-eaux-2025-08"


class TestOrganizePipeline(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        for key in ("PROCRAFILER_AI_ANALYSIS_PRIMARY", "PROCRAFILER_AI_ORGANIZE_PRIMARY"):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _drop_claim_folder(self) -> None:
        sub = self.paths.inbox_dir / "Dégats_eaux"
        sub.mkdir()
        (sub / "constat.txt").write_bytes(b"constat amiable de degat des eaux")
        (sub / "photo.txt").write_bytes(b"description d'une photo de moisissure")

    def _run(self) -> dict[str, int]:
        # Per-file analysis sends both to Insurance; the organize pass groups them.
        analysis = json.dumps({"name": "Doc", "category_path": "Personal/Administrative/Insurance", "summary": "sinistre"})
        organize = json.dumps({"placements": [{"index": 0, "path": AFFAIR}, {"index": 1, "path": AFFAIR}]})
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
            with patch("procrafiler.ai_organize.call_mistral_chat", return_value=organize):
                return process_all_inbox_files(self.paths, now_utc=self.now)

    def _files_under(self, *parts: str) -> list[Path]:
        base = self.paths.library_root.joinpath(*parts)
        return [p for p in base.rglob("*") if p.is_file() and not p.is_symlink()] if base.exists() else []

    def test_set_is_grouped_into_a_dated_affair_folder(self) -> None:
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        self._drop_claim_folder()
        summary = self._run()

        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["organized"], 2)
        # Both files end up grouped in the dated affair folder, not bare in Insurance/.
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Insurance", "Degats-eaux-2025-08")), 2)
        directly_in_insurance = [
            p for p in (self.paths.library_root / "Personal" / "Administrative" / "Insurance").iterdir() if p.is_file()
        ]
        self.assertEqual(directly_in_insurance, [])

    def test_catalog_and_mirror_follow_the_grouping(self) -> None:
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        self._drop_claim_folder()
        self._run()

        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        for doc in repo.list_documents():
            self.assertIn("Insurance/Degats-eaux-2025-08", doc["current_path"])
            self.assertEqual(json.loads(doc["content_json"])["category_path"], AFFAIR)
        # The mirror copies moved with them.
        mirror_affair = [
            p for p in (self.paths.mirror_root / "Personal" / "Administrative" / "Insurance" / "Degats-eaux-2025-08").rglob("*") if p.is_file()
        ]
        self.assertEqual(len(mirror_affair), 2)

    def test_no_organize_chain_means_no_grouping(self) -> None:
        # Without an ORGANIZE chain, there is no set phase: files stay where the
        # per-file analysis put them (bare Insurance/).
        self._drop_claim_folder()
        summary = self._run()
        self.assertEqual(summary["organized"], 0)
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Insurance")), 2)
        self.assertFalse((self.paths.library_root / "Personal" / "Administrative" / "Insurance" / "Degats-eaux-2025-08").exists())

    def test_uncertain_files_do_not_leak_when_the_set_decides(self) -> None:
        # THE core B win over the old post-pass: per-file analysis is UNCERTAIN
        # (null category + options), which ALONE would park each file in the
        # decisions queue (Manual_Review). Because the whole set is catalogued
        # first then organised together, they are placed coherently in the affair
        # folder instead — nothing leaks file-by-file.
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        self._drop_claim_folder()
        analysis = json.dumps(
            {
                "name": "Doc",
                "category_path": None,
                "alternatives": ["Personal/Administrative/Insurance", "Personal/Administrative"],
                "summary": "sinistre",
            }
        )
        organize = json.dumps({"placements": [{"index": 0, "path": AFFAIR}, {"index": 1, "path": AFFAIR}]})
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
            with patch("procrafiler.ai_organize.call_mistral_chat", return_value=organize):
                summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["pending_decisions"], 0)  # NO leak — the set decided
        self.assertEqual(summary["processed"], 2)
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Insurance", "Degats-eaux-2025-08")), 2)

    def test_organizer_sees_the_whole_set_in_one_call(self) -> None:
        # Catalog-first: the organiser is called ONCE with every fiche of the set
        # (not file-by-file), and the prompt carries the drop-folder hypothesis.
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        self._drop_claim_folder()
        analysis = json.dumps({"name": "Doc", "category_path": "Personal/Administrative/Insurance", "summary": "x"})
        organize = json.dumps({"placements": [{"index": 0, "path": AFFAIR}, {"index": 1, "path": AFFAIR}]})
        captured: dict[str, str] = {}

        def fake_org(prompt: str, model: str, **kwargs: object) -> str:
            captured["prompt"] = prompt
            return organize

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
            with patch("procrafiler.ai_organize.call_mistral_chat", side_effect=fake_org) as organize_mock:
                process_all_inbox_files(self.paths, now_utc=self.now)

        organize_mock.assert_called_once()
        self.assertIn("[0]", captured["prompt"])
        self.assertIn("[1]", captured["prompt"])
        self.assertIn("Dégats_eaux", captured["prompt"])  # the folder is fed as a hypothesis
        self.assertIn("STRONG HYPOTHESIS", captured["prompt"])

    def test_an_off_topic_file_is_split_out_of_the_set(self) -> None:
        # The folder is a hypothesis, not binding: the organiser may place a file
        # that doesn't belong somewhere else entirely (content has the last word).
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        self._drop_claim_folder()
        banking = "Personal/Administrative/Banking"
        analysis = json.dumps({"name": "Doc", "category_path": "Personal/Administrative/Insurance", "summary": "x"})
        organize = json.dumps({"placements": [{"index": 0, "path": AFFAIR}, {"index": 1, "path": banking}]})
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
            with patch("procrafiler.ai_organize.call_mistral_chat", return_value=organize):
                process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(len(self._files_under("Personal", "Administrative", "Insurance", "Degats-eaux-2025-08")), 1)
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Banking")), 1)

    def test_huge_folder_is_organized_in_batches(self) -> None:
        # R7 scale guard: a folder larger than ORGANIZE_MAX_SET is organized in
        # several batches (not one giant call), and every file is still placed.
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        sub = self.paths.inbox_dir / "Big"
        sub.mkdir()
        for i in range(3):
            (sub / f"f{i}.txt").write_bytes(f"document numero {i}".encode())
        analysis = json.dumps({"name": "Doc", "category_path": "Personal/Administrative/Insurance", "summary": "x"})
        organize = json.dumps({"placements": [{"index": 0, "path": AFFAIR}, {"index": 1, "path": AFFAIR}]})
        with patch("procrafiler.pipeline.ORGANIZE_MAX_SET", 2):
            with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
                with patch("procrafiler.ai_organize.call_mistral_chat", return_value=organize) as organize_mock:
                    summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(organize_mock.call_count, 2)  # 3 docs, batch size 2 → 2 calls
        self.assertEqual(summary["processed"], 3)
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Insurance", "Degats-eaux-2025-08")), 3)

    def test_root_singletons_are_not_organized_as_a_set(self) -> None:
        # Files loose in the Inbox root are singletons — the set organiser is NOT
        # run over them as a group (that would invent a set the user never made).
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        (self.paths.inbox_dir / "a.txt").write_bytes(b"banking statement")
        (self.paths.inbox_dir / "b.txt").write_bytes(b"another banking note")
        analysis = json.dumps({"name": "Doc", "category_path": "Personal/Administrative/Banking", "summary": "x"})
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
            with patch("procrafiler.ai_organize.call_mistral_chat") as organize_mock:
                summary = process_all_inbox_files(self.paths, now_utc=self.now)

        organize_mock.assert_not_called()  # root singletons skip the set organiser
        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["organized"], 0)
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Banking")), 2)

    def test_user_context_is_injected_into_the_analysis_prompt(self) -> None:
        # The optional user-context file is loaded and fed to the analysis prompt
        # (PROCRAFILER_CONTEXT_FILE wins over any repo-root context.txt → deterministic).
        ctx = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
        ctx.write("La musique est ma passion (loisir, pas pro).")
        ctx.close()
        os.environ["PROCRAFILER_CONTEXT_FILE"] = ctx.name
        try:
            (self.paths.inbox_dir / "note.txt").write_bytes(b"un document a classer")
            captured: dict[str, str] = {}

            def fake_analysis(prompt: str, model: str, **kwargs: object) -> str:
                captured["prompt"] = prompt
                return json.dumps({"name": "Doc", "category_path": "Personal/Hobbies", "summary": "x"})

            with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=fake_analysis):
                process_all_inbox_files(self.paths, now_utc=self.now)

            self.assertIn("La musique est ma passion", captured["prompt"])
            self.assertIn("About the user", captured["prompt"])
        finally:
            os.environ.pop("PROCRAFILER_CONTEXT_FILE", None)
            os.unlink(ctx.name)


class TestSingletonGrouping(unittest.TestCase):
    """M2+M3: root singletons that share a series are regrouped together."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        os.environ["PROCRAFILER_AI_ORGANIZE_PRIMARY"] = "mistral:mistral-medium-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
        os.environ.pop("PROCRAFILER_AI_ORGANIZE_PRIMARY", None)
        self.tmp.cleanup()

    def _files_under(self, *parts: str) -> list[Path]:
        base = self.paths.library_root.joinpath(*parts)
        return [p for p in base.rglob("*") if p.is_file() and not p.is_symlink()] if base.exists() else []

    def _symlinks_in_library(self) -> list[Path]:
        return [p for p in self.paths.library_root.rglob("*") if p.is_symlink()]

    def test_two_singletons_of_same_series_are_regrouped(self) -> None:
        # Two "Releve eau" files dropped as root singletons.
        # First run: filed in Housing/ (no existing files → grouping skips).
        # Second run: grouping sees the first file in Housing/, proposes
        # Housing/Releves-eau as a common series, moves the first there, and
        # files the second there too. Symlink left at first file's old location.
        housing = "Personal/Administrative/Housing"
        analysis_raw = json.dumps(
            {"name": "Releve-eau", "category_path": housing, "summary": "relevé compteur eau"}
        )

        # Drop and process first singleton.
        (self.paths.inbox_dir / "releve_jan.txt").write_bytes(b"Releve compteur eau janvier 2026")
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis_raw):
            summary1 = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary1["processed"], 1)
        housing_dir = self.paths.library_root / "Personal" / "Administrative" / "Housing"
        stored = [p for p in housing_dir.rglob("*") if p.is_file() and not p.is_symlink()]
        self.assertEqual(len(stored), 1)
        first_stored = stored[0]

        # Drop second singleton.
        (self.paths.inbox_dir / "releve_fev.txt").write_bytes(b"Releve compteur eau fevrier 2026")

        series = f"{housing}/Releves-eau"
        grouping_raw = json.dumps({"path": series, "group_with": [first_stored.name]})

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis_raw):
            with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=grouping_raw):
                summary2 = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary2["processed"], 1)
        self.assertEqual(summary2["regrouped"], 1)

        # Both files end up in Housing/Releves-eau.
        series_dir = self.paths.library_root / "Personal" / "Administrative" / "Housing" / "Releves-eau"
        real_files = [p for p in series_dir.rglob("*") if p.is_file() and not p.is_symlink()]
        self.assertEqual(len(real_files), 2)

        # Symlink left at the first file's old location.
        self.assertTrue(first_stored.is_symlink(), "old location should be a symlink after regroup")
        self.assertTrue(first_stored.resolve().is_file(), "symlink should point to a real file")

        # Catalog entry for the first file updated to new path.
        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        docs = repo.list_documents()
        paths_in_catalog = {d["current_path"] for d in docs}
        # The first file is now recorded at its new location (inside Releves-eau).
        self.assertTrue(
            any("Releves-eau" in p for p in paths_in_catalog),
            "catalog should reflect the new Releves-eau path",
        )

    def test_unknown_filename_in_group_with_warns_without_crash(self) -> None:
        # If group_with names a file that doesn't exist on disk, the pipeline
        # warns but doesn't crash, and still files the new document normally.
        housing = "Personal/Administrative/Housing"
        housing_dir = self.paths.library_root / "Personal" / "Administrative" / "Housing"
        housing_dir.mkdir(parents=True, exist_ok=True)
        (housing_dir / "2026-01-01__Existing.txt").write_bytes(b"an existing file")

        (self.paths.inbox_dir / "releve.txt").write_bytes(b"Releve eau")
        analysis_raw = json.dumps(
            {"name": "Releve-eau", "category_path": housing, "summary": "compteur"}
        )
        grouping_raw = json.dumps({"path": f"{housing}/Releves-eau", "group_with": ["ghost-file.pdf"]})

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis_raw):
            with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=grouping_raw):
                summary = process_all_inbox_files(self.paths, now_utc=self.now)

        # New file is still filed (in the proposed series folder).
        self.assertEqual(summary["processed"], 1)
        # Ghost file not found → no regroup.
        self.assertEqual(summary["regrouped"], 0)
        # New file goes to the proposed Releves-eau subfolder.
        series_dir = self.paths.library_root / "Personal" / "Administrative" / "Housing" / "Releves-eau"
        self.assertTrue(series_dir.exists())
        new_files = [p for p in series_dir.rglob("*") if p.is_file() and not p.is_symlink()]
        self.assertEqual(len(new_files), 1)

    def test_series_file_gets_year_subfolder_from_document_date(self) -> None:
        # A series root singleton: analysis proposes only the ENTITY folder and
        # series=true; the pipeline appends the YEAR subfolder itself, derived
        # from the document's OWN date (2019), not the processing time (2026).
        (self.paths.inbox_dir / "facture.txt").write_bytes(b"Facture Enercoop abonnement avril 2019")
        analysis = json.dumps({
            "name": "Facture_Enercoop",
            "date": "2019-04-25",
            "category_path": "Personal/Administrative/Utilities/Enercoop",
            "series": True,
            "summary": "facture d'electricite",
        })
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
            summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["processed"], 1)
        year_dir = self.paths.library_root / "Personal" / "Administrative" / "Utilities" / "Enercoop" / "2019"
        filed = [p for p in year_dir.glob("*") if p.is_file()]
        self.assertEqual(len(filed), 1, "series file should be filed in its <Entity>/<Year> folder")

    def test_non_series_file_gets_no_year_subfolder(self) -> None:
        # series=false → the pipeline never appends a year; the file stays in the
        # folder the AI proposed.
        (self.paths.inbox_dir / "idees.txt").write_bytes(b"idees de week-end a la campagne")
        analysis = json.dumps({
            "name": "Idees-week-end",
            "date": "2026-05-30",
            "category_path": "Personal/Hobbies",
            "series": False,
            "summary": "notes loisirs",
        })
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
            process_all_inbox_files(self.paths, now_utc=self.now)

        hobbies = self.paths.library_root / "Personal" / "Hobbies"
        self.assertEqual(len(self._files_under("Personal", "Hobbies")), 1)
        # No bare-year subfolder was created under Hobbies.
        self.assertFalse(any(p.is_dir() and p.name.isdigit() for p in hobbies.iterdir()))

    def test_series_at_a_bare_base_is_not_dated(self) -> None:
        # A series doc the AI under-routes flat into a BASE (no entity folder)
        # is NOT dated — there is no entity folder to date.
        (self.paths.inbox_dir / "certif.txt").write_bytes(b"attestation de formation")
        analysis = json.dumps({
            "name": "Certificat_Formation",
            "date": "2023-03-26",
            "category_path": "Personal/Education",
            "series": True,
            "summary": "certificat",
        })
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis):
            process_all_inbox_files(self.paths, now_utc=self.now)

        education = self.paths.library_root / "Personal" / "Education"
        self.assertEqual(len(self._files_under("Personal", "Education")), 1)
        self.assertFalse(any(p.is_dir() and p.name.isdigit() for p in education.iterdir()))

    def test_grouping_name_renames_new_file_to_match_series(self) -> None:
        # 3a: a new singleton joins a populated series; grouping DEEPENS the
        # branch into Releves-eau and returns a consistent stem ("Releve_eau")
        # so the new file is named like its siblings instead of keeping its own
        # analysis name ("Releve-compteur").
        housing = "Personal/Administrative/Housing"
        series_dir = self.paths.library_root / "Personal" / "Administrative" / "Housing" / "Releves-eau"
        series_dir.mkdir(parents=True, exist_ok=True)
        (series_dir / "2026-01-01__Releve_eau.txt").write_bytes(b"an existing reading")

        (self.paths.inbox_dir / "nouveau.txt").write_bytes(b"Releve compteur eau mars 2026")
        # Analysis routes to the parent Housing; grouping deepens into Releves-eau.
        analysis_raw = json.dumps(
            {"name": "Releve-compteur", "category_path": housing, "summary": "compteur"}
        )
        grouping_raw = json.dumps(
            {"path": f"{housing}/Releves-eau", "group_with": [], "name": "Releve_eau"}
        )
        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis_raw):
            with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=grouping_raw):
                summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["processed"], 1)
        real_files = [p for p in series_dir.rglob("*") if p.is_file() and not p.is_symlink()]
        self.assertEqual(len(real_files), 2)
        # The NEW file carries the series-consistent stem, not its analysis name.
        new_file = next(p for p in real_files if p.name != "2026-01-01__Releve_eau.txt")
        self.assertIn("Releve_eau", new_file.name)
        self.assertNotIn("Releve-compteur", new_file.name)

    def test_same_run_regroup_leaves_no_symlink(self) -> None:
        # G5 + G2: two compteurs dropped as root singletons in the SAME run, on
        # an empty library. The first is filed bare in Housing; the second's
        # analysis proposes the series folder (which doesn't exist on disk →
        # its existing ANCESTOR Housing becomes the candidate branch, G2), and
        # the grouping pulls the first file down into it. Both files were
        # placed THIS run → there was no pre-run reference to preserve →
        # ZERO symlink in the library (the run-3 design bug).
        housing = "Personal/Administrative/Housing"
        series = f"{housing}/Releves-eau"
        analyses = iter(
            [
                json.dumps({"name": "Releve-eau", "category_path": housing, "summary": "compteur janvier"}),
                json.dumps({"name": "Releve-eau", "category_path": series, "summary": "compteur fevrier"}),
            ]
        )
        # group_with cites the bare file name without its timestamp prefix —
        # the unique prefix-tolerant match finds it among the listed files.
        grouping_raw = json.dumps({"path": series, "group_with": ["Releve-eau.txt"]})
        (self.paths.inbox_dir / "a_releve.txt").write_bytes(b"Releve compteur eau janvier")
        (self.paths.inbox_dir / "b_releve.txt").write_bytes(b"Releve compteur eau fevrier")

        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=lambda *a, **k: next(analyses)):
            with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=grouping_raw):
                summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["regrouped"], 1)
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Housing", "Releves-eau")), 2)
        self.assertEqual(self._symlinks_in_library(), [])  # nothing pre-run → no marker

    def test_flattening_path_is_ignored_and_analysis_route_kept(self) -> None:
        # G3: the grouping answers with a candidate branch ROOT (not a deeper
        # subfolder). That's a flatten — it is ignored: the new file keeps its
        # analysis route (the series folder), and nothing is regrouped.
        housing = "Personal/Administrative/Housing"
        series = f"{housing}/Releves-eau"
        housing_dir = self.paths.library_root / "Personal" / "Administrative" / "Housing"
        (housing_dir / "2026-01-01_00-00-00__Constat.txt").write_bytes(b"x")  # branch non-empty → grouping runs

        (self.paths.inbox_dir / "releve.txt").write_bytes(b"Releve compteur eau")
        analysis_raw = json.dumps({"name": "Releve-eau", "category_path": series, "summary": "compteur"})
        grouping_raw = json.dumps({"path": housing, "group_with": ["2026-01-01_00-00-00__Constat.txt"]})

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis_raw):
            with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=grouping_raw):
                summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["regrouped"], 0)
        # The analysis route (series folder) survived the flattening attempt.
        self.assertEqual(len(self._files_under("Personal", "Administrative", "Housing", "Releves-eau")), 1)
        self.assertTrue((housing_dir / "2026-01-01_00-00-00__Constat.txt").is_file())

    def test_regroup_refuses_to_pull_a_file_out_of_its_subfolder(self) -> None:
        # G4: group_with cites a file living in a dated affair subfolder; the
        # proposed series folder is NOT a descendant of that subfolder → the
        # move would de-organize → refused (file stays), new file still filed.
        housing = "Personal/Administrative/Housing"
        affair_dir = self.paths.library_root / "Personal" / "Administrative" / "Housing" / "2025-08-Degats-eaux"
        affair_dir.mkdir(parents=True)
        affair_file = affair_dir / "2025-08-01_00-00-00__Constat.txt"
        affair_file.write_bytes(b"constat")
        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        repo.upsert_document(
            doc_id="doc-affair",
            sha256="cafe",
            current_filename=affair_file.name,
            current_path=str(affair_file),
            status="LIBRARY_STORED",
            updated_at_utc="2025-08-01T00:00:00Z",
            flow_state="LIBRARY_STORED",
        )

        (self.paths.inbox_dir / "releve.txt").write_bytes(b"Releve compteur eau")
        analysis_raw = json.dumps({"name": "Releve-eau", "category_path": housing, "summary": "compteur"})
        grouping_raw = json.dumps(
            {
                "path": f"{housing}/Releves-eau",
                "group_with": ["2025-08-Degats-eaux/2025-08-01_00-00-00__Constat.txt"],
            }
        )

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=analysis_raw):
            with patch("procrafiler.ai_grouping.call_mistral_chat", return_value=grouping_raw):
                summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["regrouped"], 0)
        self.assertTrue(affair_file.is_file())  # the affair folder kept its document
        self.assertEqual(self._symlinks_in_library(), [])
        events = [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(any(e["action"] == "regroup_refused_not_deeper" for e in events))


if __name__ == "__main__":
    unittest.main()
