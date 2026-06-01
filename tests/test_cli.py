# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.cli import main  # type: ignore[reportMissingImports]
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_next_inbox_file


class TestCliMirrorPurge(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_FAKE_NOW", None)
        self.tmp.cleanup()

    def test_purge_mirror_trash_cli(self) -> None:
        old_file = self.paths.mirror_trash_dir / "old.txt"
        new_file = self.paths.mirror_trash_dir / "new.txt"
        old_file.parent.mkdir(parents=True, exist_ok=True)

        old_file.write_text("old", encoding="utf-8")
        new_file.write_text("new", encoding="utf-8")

        now = datetime(2026, 4, 2, 12, 0, 0, tzinfo=timezone.utc)
        os.environ["PROCRAFILER_FAKE_NOW"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        old_ts = (now - timedelta(days=40)).timestamp()
        new_ts = (now - timedelta(days=2)).timestamp()
        os.utime(old_file, (old_ts, old_ts))
        os.utime(new_file, (new_ts, new_ts))

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["purge-mirror-trash", "--days", "30"])

        self.assertEqual(code, 0)
        self.assertIn("removed: 1", out.getvalue())
        self.assertFalse(old_file.exists())
        self.assertTrue(new_file.exists())

        events = [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(any(event["action"] == "mirror_trash_purge" for event in events))

    def test_policy_effective_cli_reads_override(self) -> None:
        self.paths.policy_file.write_text(
            "[mirror]\nretention_days = 14\nversions_keep = 4\n\n[taxonomy]\nmax_depth = 7\n",
            encoding="utf-8",
        )

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["policy-effective"])

        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("ProcraFiler policy effective", text)
        self.assertIn("mirror_retention_days: 14", text)
        self.assertIn("mirror_versions_keep: 4", text)
        self.assertIn("taxonomy_max_depth: 7", text)

    def test_purge_mirror_trash_cli_uses_policy_default(self) -> None:
        self.paths.policy_file.write_text(
            "[mirror]\nretention_days = 1\nversions_keep = 3\n\n[taxonomy]\nmax_depth = 6\n",
            encoding="utf-8",
        )

        old_file = self.paths.mirror_trash_dir / "old-policy.txt"
        old_file.parent.mkdir(parents=True, exist_ok=True)
        old_file.write_text("old", encoding="utf-8")

        # Set mtime to two days ago so policy retention_days=1 should purge it.
        two_days_ago = (datetime.now(timezone.utc) - timedelta(days=2)).timestamp()
        os.utime(old_file, (two_days_ago, two_days_ago))

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["purge-mirror-trash"])

        self.assertEqual(code, 0)
        self.assertIn("removed: 1", out.getvalue())
        self.assertFalse(old_file.exists())


class TestCliReview(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_FAKE_NOW", None)
        os.environ.pop("PROCRAFILER_AI_CLASSIFICATION_PRIMARY", None)
        self.tmp.cleanup()

    def _park_one(self) -> None:
        os.environ["PROCRAFILER_AI_CLASSIFICATION_PRIMARY"] = "mistral:mistral-small-latest"
        (self.paths.inbox_dir / "lettre.txt").write_bytes(b"contenu ambigu")
        now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)
        with patch(
            "procrafiler.ai_classification.call_mistral_chat",
            return_value=json.dumps({"path": None, "alternatives": ["Banque", "Administratif/Impots"]}),
        ):
            process_next_inbox_file(self.paths, now_utc=now)

    def test_review_no_pending(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["review"])
        self.assertEqual(code, 0)
        self.assertIn("No decisions pending.", out.getvalue())

    def test_review_resolves_via_cli_stdin(self) -> None:
        self._park_one()
        out = io.StringIO()
        with redirect_stdout(out), patch("builtins.input", return_value="1"):
            code = main(["review"])
        self.assertEqual(code, 0)
        self.assertIn("resolved 1", out.getvalue())
        filed = [p for p in (self.paths.library_root / "Banque").rglob("*") if p.is_file()]
        self.assertEqual(len(filed), 1)


if __name__ == "__main__":
    unittest.main()
