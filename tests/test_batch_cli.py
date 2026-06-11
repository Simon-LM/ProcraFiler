# pyright: reportUnknownVariableType=false, reportUnknownMemberType=false
from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from procrafiler.cli import main  # type: ignore[reportMissingImports]
from procrafiler.config import default_runtime_paths, ensure_runtime_layout


class TestBatchCli(unittest.TestCase):
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
        self.tmp.cleanup()

    def test_process_all_batch_summary(self) -> None:
        (self.paths.inbox_dir / "a.txt").write_bytes(b"alpha")
        (self.paths.inbox_dir / "b.txt").write_bytes(b"beta")
        (self.paths.inbox_dir / "c.txt").write_bytes(b"alpha")

        out = io.StringIO()
        with redirect_stdout(out):
            code = main(["process-all"])

        self.assertEqual(code, 0)
        text = out.getvalue()
        self.assertIn("processed: 2", text)
        self.assertIn("duplicates: 1", text)
        self.assertIn("manual_reviews: 0", text)
        self.assertIn("errors: 0", text)
        self.assertIn("mirror_failures: 0", text)

        self.assertEqual(len(list(self.paths.inbox_dir.iterdir())), 0)
        self.assertEqual(len(list(self.paths.queue_dir.iterdir())), 0)
        self.assertEqual(len(list(self.paths.inbox_trash_manual_dir.iterdir())), 1)
        library_files = [p for p in self.paths.library_root.rglob("*") if p.is_file() and not p.is_symlink()]
        self.assertEqual(len(library_files), 2)

        events = [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(any(event["action"] == "process_all_summary" for event in events))


if __name__ == "__main__":
    unittest.main()
