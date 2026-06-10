# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.user_context import MAX_CONTEXT_CHARS, load_user_context


class TestUserContext(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        # Run from a clean temp dir so the developer's own repo-root context.txt
        # never leaks into these tests.
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self._saved = {k: os.environ.get(k) for k in ("PROCRAFILER_CONTEXT_FILE", "PROCRAFILER_CONFIG_HOME")}
        os.environ.pop("PROCRAFILER_CONTEXT_FILE", None)
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(self.root / "cfg")

    def tearDown(self) -> None:
        os.chdir(self._cwd)
        for key, value in self._saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self.tmp.cleanup()

    def test_absent_returns_none(self) -> None:
        self.assertIsNone(load_user_context())

    def test_reads_cwd_context_txt(self) -> None:
        (self.root / "context.txt").write_text("[Loisirs]\nLa musique est ma passion", encoding="utf-8")
        self.assertIn("musique", load_user_context() or "")

    def test_strips_hash_comments_and_html_comments(self) -> None:
        (self.root / "context.txt").write_text(
            "# guidance to ignore\n<!-- md guidance -->\n[Travail]\nDeveloppeur web", encoding="utf-8"
        )
        ctx = load_user_context() or ""
        self.assertNotIn("guidance", ctx)
        self.assertIn("Developpeur web", ctx)
        self.assertIn("[Travail]", ctx)  # section labels are kept

    def test_only_comments_returns_none(self) -> None:
        (self.root / "context.txt").write_text("# just a comment\n<!-- nothing -->\n   \n", encoding="utf-8")
        self.assertIsNone(load_user_context())

    def test_explicit_env_path_wins(self) -> None:
        elsewhere = self.root / "elsewhere.txt"
        elsewhere.write_text("Contexte explicite", encoding="utf-8")
        os.environ["PROCRAFILER_CONTEXT_FILE"] = str(elsewhere)
        (self.root / "context.txt").write_text("ignore me", encoding="utf-8")
        self.assertEqual(load_user_context(), "Contexte explicite")

    def test_falls_back_to_config_home(self) -> None:
        cfg = self.root / "cfg"
        cfg.mkdir()
        (cfg / "context.txt").write_text("Contexte de config", encoding="utf-8")
        self.assertEqual(load_user_context(), "Contexte de config")

    def test_caps_length(self) -> None:
        (self.root / "context.txt").write_text("x" * (MAX_CONTEXT_CHARS + 500), encoding="utf-8")
        self.assertEqual(len(load_user_context() or ""), MAX_CONTEXT_CHARS)


if __name__ == "__main__":
    unittest.main()
