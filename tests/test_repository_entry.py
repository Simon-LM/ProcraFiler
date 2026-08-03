# pyright: reportUnknownVariableType=false
"""A repository is one object, and gets one catalog entry.

Measured on ProcraFiler's own tree before this existed: 33 readable files, 33
paid analysis calls — one per `.md`, `.txt`, `.sh` and `.pdf` under 2 MB. That
figure is entirely a property of the repository; a project with a committed
`node_modules` runs to thousands. And every one of those calls produced a fiche
nobody would ever search for: people look for the project, not for the third
paragraph of a helper module.

Two things must hold whatever else changes. **The history is never read** — no
commit, no blob, no branch — and **the files are never touched**: a repository in
the library is there because someone wants it kept exactly as it is.
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from procrafiler.catalog import CatalogRepository
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import (
    _repository_description,
    _repository_inventory,
    _repository_readme,
    run_rescan,
)
from procrafiler.rescan import DELETED_STATUS, walk_repository_roots


def _make_repo(root: Path, *, readme: str | None = "# Superbe projet\n\nUn outil de sauvegarde.") -> Path:
    (root / ".git").mkdir(parents=True, exist_ok=True)
    (root / ".git" / "HEAD").write_text("ref: refs/heads/main", encoding="utf-8")
    (root / ".git" / "COMMIT_EDITMSG").write_text("secret commit message", encoding="utf-8")
    (root / "src").mkdir(parents=True, exist_ok=True)
    (root / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
    (root / "docs").mkdir(parents=True, exist_ok=True)
    (root / "docs" / "guide.md").write_text("# Guide\nRevocation procedure.", encoding="utf-8")
    (root / "run.sh").write_text("#!/bin/sh\necho ok", encoding="utf-8")
    if readme is not None:
        (root / "README.md").write_text(readme, encoding="utf-8")
    return root


class DetectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.library = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_a_repository_is_found_wherever_the_user_put_it(self) -> None:
        """Not only in Archive. A repository must be treated as one object whether
        it sits three folders down or at the library root."""
        deep = _make_repo(self.library / "Work" / "Business" / "backup" / "tool")
        shallow = _make_repo(self.library / "at-the-top")
        self.assertEqual(walk_repository_roots(self.library), sorted([deep, shallow]))

    def test_a_git_FILE_counts_as_much_as_a_git_directory(self) -> None:
        """`.git` is a file in a worktree and in a submodule. Matching only
        directories would read those two file by file."""
        worktree = self.library / "worktree"
        worktree.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /elsewhere/.git/worktrees/x", encoding="utf-8")
        self.assertEqual(walk_repository_roots(self.library), [worktree])

    def test_a_library_without_repositories_finds_none(self) -> None:
        (self.library / "Personal").mkdir(parents=True)
        self.assertEqual(walk_repository_roots(self.library), [])

    def test_a_missing_library_is_not_an_error(self) -> None:
        self.assertEqual(walk_repository_roots(self.library / "nope"), [])


class DescriptionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.library = Path(self.tmp.name)
        self.repo = _make_repo(self.library / "Personal" / "Archive" / "superbe-projet")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_the_readme_is_what_says_what_the_project_is(self) -> None:
        self.assertIn("Un outil de sauvegarde", _repository_readme(self.repo))

    def test_a_repository_without_a_readme_says_so(self) -> None:
        """"No README" and "I did not look" must not read the same."""
        bare = _make_repo(self.library / "bare", readme=None)
        self.assertEqual(_repository_readme(bare), "")
        self.assertIn("no README", _repository_description(bare, self.library))

    def test_a_very_long_readme_is_truncated(self) -> None:
        """A long README is a manual, and the rest of the manual adds nothing a
        catalog entry can use."""
        long_one = _make_repo(self.library / "verbose", readme="z" * 20_000)
        self.assertLessEqual(len(_repository_readme(long_one)), 4000)

    def test_the_inventory_counts_the_working_tree_and_skips_the_history(self) -> None:
        total, top = _repository_inventory(self.repo)
        self.assertEqual(total, 4, "README + main.py + guide.md + run.sh")
        self.assertIn(("md", 2), top)

    def test_the_description_names_the_project_and_its_shape(self) -> None:
        text = _repository_description(self.repo, self.library)
        self.assertIn("superbe-projet", text)
        self.assertIn("4 file(s)", text)
        self.assertIn("Un outil de sauvegarde", text)

    def test_it_says_outright_that_nothing_was_read_file_by_file(self) -> None:
        """Otherwise a model handed a README will happily summarise an
        implementation it never saw, and the invention reaches the search index."""
        text = _repository_description(self.repo, self.library).lower()
        self.assertIn("not read individually", text)
        self.assertIn("history was not read", text)

    def test_the_history_never_reaches_the_description(self) -> None:
        """The single most important line in this file."""
        text = _repository_description(self.repo, self.library)
        self.assertNotIn("secret commit message", text)
        self.assertNotIn("refs/heads/main", text)


class RescanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.repo = _make_repo(self.paths.library_root / "Personal" / "Archive" / "superbe-projet")

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
        self.tmp.cleanup()

    _REPLY = json.dumps({
        "name": "Superbe projet", "category_path": "Personal/Archive", "date": None,
        "series": False, "summary": "Un outil de sauvegarde.",
        "keywords": ["python", "sauvegarde"], "entities": {}, "language": "fr",
    })

    def _rescan(self, times: int = 1) -> int:
        calls = 0
        for _ in range(times):
            with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._REPLY) as call:
                run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)
                calls += call.call_count
        return calls

    def _live(self) -> list[dict[str, object]]:
        return [
            r for r in CatalogRepository(self.paths.catalog_db_file).list_documents()
            if r.get("status") != DELETED_STATUS
        ]

    def test_one_repository_costs_one_ai_call(self) -> None:
        self.assertEqual(self._rescan(), 1)

    def test_its_files_are_not_catalogued_individually(self) -> None:
        self._rescan()
        live = self._live()
        self.assertEqual(len(live), 1)
        self.assertEqual(str(live[0]["current_path"]), str(self.repo))

    def test_nothing_in_the_tree_is_renamed_or_moved(self) -> None:
        before = sorted(p.name for p in self.repo.rglob("*") if p.is_file())
        self._rescan()
        self.assertEqual(sorted(p.name for p in self.repo.rglob("*") if p.is_file()), before)

    def test_the_entry_survives_repeated_rescans(self) -> None:
        """The preserve-zone bug, applied to a directory row: without its own path
        being counted as present it would be declared deleted on the second pass."""
        self._rescan(3)
        live = self._live()
        self.assertEqual(len(live), 1, f"the repository entry was lost: {live}")

    def test_a_second_rescan_costs_nothing(self) -> None:
        self._rescan()
        self.assertEqual(self._rescan(2), 0)

    def test_the_fiche_is_marked_as_a_repository(self) -> None:
        self._rescan()
        fiche = json.loads(str(self._live()[0]["content_json"]))
        self.assertTrue(fiche["repository"])
        self.assertEqual(fiche["read_via"], "repository")
        self.assertEqual(fiche["name"], "superbe-projet", "the user's own folder name")
        self.assertIn("sauvegarde", fiche["keywords"])

    def test_the_fiche_keeps_the_folder_name_even_when_the_ai_proposes_one(self) -> None:
        """Asserted on the indexing step itself, not through the whole rescan: the
        later name-sync phase rewrites the fiche name from the on-disk name anyway
        and would mask a regression here. A repository is known by its folder name."""
        from procrafiler.pipeline import _index_repository

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._REPLY):
            _index_repository(
                self.paths, self.repo, now_utc=None, features={}, emit=lambda _m: None
            )
        fiche = json.loads(str(self._live()[0]["content_json"]))
        self.assertEqual(fiche["name"], "superbe-projet", "the AI's title replaced the folder name")

    def test_two_repositories_are_two_entries(self) -> None:
        _make_repo(self.paths.library_root / "Work" / "Archive" / "autre-projet")
        self.assertEqual(self._rescan(), 2)
        self.assertEqual(len(self._live()), 2)

    def test_an_archived_document_beside_a_repository_is_still_read(self) -> None:
        """The neighbouring behaviour must not be caught by this change: an Archive
        folder holds documents, and searching them is why the zone exists."""
        note = self.paths.library_root / "Personal" / "Archive" / "vieille-note.txt"
        note.write_text("Facture EDF du 30 avril 2026", encoding="utf-8")

        seen: list[str] = []

        def capture(prompt: str, *_a: object, **_k: object) -> str:
            seen.append(prompt)
            return self._REPLY

        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=capture):
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)

        self.assertTrue(any("Facture EDF" in p for p in seen), "the archived document was not read")
        self.assertIsNotNone(
            CatalogRepository(self.paths.catalog_db_file).find_by_current_path(str(note))
        )

    def test_no_file_of_the_repository_reaches_the_analysis(self) -> None:
        """The cost this exists to remove, asserted on the prompts themselves."""
        seen: list[str] = []

        def capture(prompt: str, *_a: object, **_k: object) -> str:
            seen.append(prompt)
            return self._REPLY

        with patch("procrafiler.ai_analysis.call_mistral_chat", side_effect=capture):
            run_rescan(self.paths, now_utc=None, features={}, emit=lambda _m: None)

        self.assertEqual(len(seen), 1)
        self.assertNotIn("Revocation procedure", seen[0], "docs/guide.md was read as a document")
        self.assertNotIn("secret commit message", seen[0])


if __name__ == "__main__":
    unittest.main()
