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
        return [p for p in base.rglob("*") if p.is_file()] if base.exists() else []

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


if __name__ == "__main__":
    unittest.main()
