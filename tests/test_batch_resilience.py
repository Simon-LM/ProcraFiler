# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler import pipeline
from procrafiler.ai_naming import ProviderCallError, call_mistral_chat
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_all_inbox_files


class TestNetworkErrorsAreRetryable(unittest.TestCase):
    """A socket/network failure must surface as a retryable ProviderCallError,
    not a raw exception that escapes the readers' retry+failover and crashes."""

    def setUp(self) -> None:
        os.environ["MISTRAL_API_KEY"] = "test-key"

    def tearDown(self) -> None:
        os.environ.pop("MISTRAL_API_KEY", None)

    def test_socket_timeout_becomes_provider_error(self) -> None:
        with patch("procrafiler.ai_naming.urlopen", side_effect=TimeoutError("read operation timed out")):
            with self.assertRaises(ProviderCallError) as ctx:
                call_mistral_chat("hello", "mistral-small-latest", timeout=1)
        self.assertIn("NETWORK_ERROR", str(ctx.exception))

    def test_connection_error_becomes_provider_error(self) -> None:
        with patch("procrafiler.ai_naming.urlopen", side_effect=ConnectionResetError("reset")):
            with self.assertRaises(ProviderCallError):
                call_mistral_chat("hello", "mistral-small-latest", timeout=1)


class TestBatchSurvivesAFailingFile(unittest.TestCase):
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

    def test_one_files_exception_does_not_abort_the_batch(self) -> None:
        # The first file blows up unexpectedly during cataloguing; the batch must
        # log + count it and keep going, filing the rest. (The risky steps run
        # after the file has left the Inbox, so it's already consumed.)
        (self.paths.inbox_dir / "a.txt").write_bytes(b"alpha")
        (self.paths.inbox_dir / "b.txt").write_bytes(b"beta")

        real_catalog = pipeline._catalog_one_inbox_file
        state = {"calls": 0}

        def flaky(*args: object, **kwargs: object):  # noqa: ANN202
            state["calls"] += 1
            if state["calls"] == 1:
                raise RuntimeError("boom — e.g. an unhandled reader crash")
            return real_catalog(*args, **kwargs)  # type: ignore[arg-type]

        with patch("procrafiler.pipeline._catalog_one_inbox_file", side_effect=flaky):
            summary = process_all_inbox_files(self.paths, now_utc=self.now)

        self.assertEqual(summary["errors"], 1)
        self.assertEqual(summary["processed"], 1)  # the second file still gets filed

    def test_process_error_is_logged(self) -> None:
        import json

        (self.paths.inbox_dir / "doc.txt").write_bytes(b"kaboom-bait")
        with patch("procrafiler.pipeline._catalog_one_inbox_file", side_effect=RuntimeError("kaboom")):
            process_all_inbox_files(self.paths, now_utc=self.now)

        events = [
            json.loads(line)
            for line in self.paths.actions_log_file.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(any(e["action"] == "process_error" and "kaboom" in e["message"] for e in events))


if __name__ == "__main__":
    unittest.main()
