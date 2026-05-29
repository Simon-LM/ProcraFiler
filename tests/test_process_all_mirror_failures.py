# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.config import default_runtime_paths, ensure_runtime_layout, set_feature_flag
from procrafiler.mirror import MirrorSyncResult
from procrafiler.pipeline import _process_next_inbox_file, process_all_inbox_files


class TestProcessAllMirrorFailures(unittest.TestCase):
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
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _failing_sync(self, paths, library_file, *, now_utc=None):  # noqa: ANN001
        return MirrorSyncResult(success=False, mirror_target=paths.mirror_root, error="forced")

    def test_batch_counts_real_mirror_failure_inline(self) -> None:
        (self.paths.inbox_dir / "a.txt").write_bytes(b"alpha")
        (self.paths.inbox_dir / "b.txt").write_bytes(b"beta")

        with patch("procrafiler.pipeline.sync_library_file_to_mirror", side_effect=self._failing_sync):
            summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["processed"], 2)
        self.assertEqual(summary["mirror_failures"], 2)

    def test_mirror_disabled_is_not_counted_as_failure(self) -> None:
        # The key distinction the refactor must preserve: a skipped mirror
        # (feature off) is NOT a failure. The old code scanned the log for
        # mirror_sync_failed events, which are never emitted on a skip.
        set_feature_flag(self.paths, "mirror_sync", False)
        (self.paths.inbox_dir / "a.txt").write_bytes(b"alpha")

        summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["processed"], 1)
        self.assertEqual(summary["mirror_failures"], 0)

    def test_single_file_result_carries_mirror_failed_flag(self) -> None:
        (self.paths.inbox_dir / "a.txt").write_bytes(b"alpha")

        with patch("procrafiler.pipeline.sync_library_file_to_mirror", side_effect=self._failing_sync):
            result = _process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(result.flow_state, "LIBRARY_STORED")
        self.assertTrue(result.mirror_failed)

    def test_single_file_result_no_mirror_failure_on_success(self) -> None:
        (self.paths.inbox_dir / "a.txt").write_bytes(b"alpha")

        result = _process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertEqual(result.flow_state, "LIBRARY_STORED")
        self.assertFalse(result.mirror_failed)

    def test_duplicate_result_has_no_mirror_failure(self) -> None:
        (self.paths.inbox_dir / "a.txt").write_bytes(b"same")
        _process_next_inbox_file(self.paths, now_utc=self.now)
        (self.paths.inbox_dir / "b.txt").write_bytes(b"same")

        result = _process_next_inbox_file(self.paths, now_utc=self.now)
        self.assertEqual(result.flow_state, "INBOX_TRASH_PENDING_MANUAL")
        self.assertFalse(result.mirror_failed)


if __name__ == "__main__":
    unittest.main()
