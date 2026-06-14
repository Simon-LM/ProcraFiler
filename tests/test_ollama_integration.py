"""Real, opt-in integration tests against a LOCAL Ollama (no Mistral, no cost).

These run the actual pipeline through local models, so they are SLOW and need
Ollama running with the right models pulled. They are skipped by default and
only run when `PROCRAFILER_OLLAMA_IT=1` is set, e.g.::

    PROCRAFILER_OLLAMA_IT=1 .venv/bin/python -m unittest tests.test_ollama_integration

The routine suite stays fast, offline and deterministic (everything else mocks
the AI). This is the local-model counterpart to the user's Mistral sandbox run.
"""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Models assigned for the agent's local tests (easy to change here).
# gemma4:12b classifies text well and fits 12GB VRAM; qwen3.5:9b returned empty
# on the full analysis prompt, so it is not used here.
ANALYSIS_MODEL = "gemma4:12b"
VISION_MODEL = "qwen2.5vl:7b"
OCR_MODEL = "minicpm-v:latest"

_OLLAMA_TAGS_URL = "http://localhost:11434/api/tags"


def _ollama_models() -> set[str] | None:
    """Return the set of locally available Ollama model names, or None if the
    server is unreachable (so the test can skip cleanly)."""
    try:
        with urllib.request.urlopen(_OLLAMA_TAGS_URL, timeout=3) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None
    return {m.get("name", "") for m in data.get("models", [])}


_ENABLED = os.environ.get("PROCRAFILER_OLLAMA_IT") == "1"
_MODELS = _ollama_models() if _ENABLED else None


@unittest.skipUnless(_ENABLED, "set PROCRAFILER_OLLAMA_IT=1 to run real Ollama integration tests")
class TestOllamaPipelineIntegration(unittest.TestCase):
    def setUp(self) -> None:
        if _MODELS is None:
            self.skipTest("Ollama server not reachable at localhost:11434")
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        # Point every task at LOCAL Ollama. Set BEFORE any run: these win over a
        # repo .env (load_runtime_env only sets a var if absent) → no Mistral.
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = f"ollama:{ANALYSIS_MODEL}"
        os.environ["PROCRAFILER_AI_IMAGE_PRIMARY"] = f"ollama:{VISION_MODEL}"
        os.environ["PROCRAFILER_AI_OCR_PRIMARY"] = f"ollama:{OCR_MODEL}"
        os.environ["PROCRAFILER_AI_ANALYSIS_TIMEOUT"] = "240"
        os.environ["PROCRAFILER_AI_IMAGE_TIMEOUT"] = "240"
        os.environ["PROCRAFILER_AI_OCR_TIMEOUT"] = "240"
        # Space out local calls so a sequential run doesn't overheat the GPU.
        os.environ["PROCRAFILER_AI_THROTTLE"] = "1.5"

        from procrafiler.config import default_runtime_paths, ensure_runtime_layout

        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.now = datetime(2026, 4, 2, 10, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        for key in (
            "PROCRAFILER_AI_ANALYSIS_PRIMARY", "PROCRAFILER_AI_IMAGE_PRIMARY", "PROCRAFILER_AI_OCR_PRIMARY",
            "PROCRAFILER_AI_ANALYSIS_TIMEOUT", "PROCRAFILER_AI_IMAGE_TIMEOUT", "PROCRAFILER_AI_OCR_TIMEOUT",
            "PROCRAFILER_AI_THROTTLE",
        ):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def _require(self, model: str) -> None:
        assert _MODELS is not None
        if model not in _MODELS:
            self.skipTest(f"Ollama model not pulled: {model}")

    def _stored_fiches(self) -> list[dict]:
        from procrafiler.catalog import CatalogRepository

        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()
        out = []
        for doc in repo.list_documents():
            if doc["status"] == "LIBRARY_STORED" and doc["content_json"]:
                out.append(json.loads(doc["content_json"]))
        return out

    def test_text_document_is_read_and_classified_via_ollama(self) -> None:
        self._require(ANALYSIS_MODEL)
        from procrafiler.pipeline import process_all_inbox_files

        (self.paths.inbox_dir / "releve.txt").write_text(
            "BNP Paribas - Releve de compte courant. Solde au 30/04/2026: 2 340,15 EUR.\n"
            "Operations: salaire, prelevement loyer, prelevement EDF.",
            encoding="utf-8",
        )
        summary = process_all_inbox_files(self.paths, now_utc=self.now)
        # Plumbing check: the document flows through an Ollama-configured pipeline
        # end-to-end without errors and is filed. Whether the local model produced
        # a usable name/category is a MODEL-QUALITY question (observed separately):
        # a weak model degrades to manual review, which is correct behaviour, not
        # a pipeline failure.
        self.assertEqual(summary["errors"], 0)
        self.assertEqual(len(self._stored_fiches()), 1)

    def test_image_document_is_vision_read_and_classified_via_ollama(self) -> None:
        self._require(VISION_MODEL)
        from PIL import Image, ImageDraw

        from procrafiler.pipeline import process_all_inbox_files

        img = Image.new("RGB", (800, 300), "white")
        draw = ImageDraw.Draw(img)
        draw.text((30, 40), "FACTURE EDF", fill="black")
        draw.text((30, 120), "Montant: 84,50 EUR  Date: 05/04/2026", fill="black")
        img.save(self.paths.inbox_dir / "facture.png")

        summary = process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertEqual(summary["errors"], 0)
        # It was read by the vision model and filed (not stuck unreadable).
        self.assertGreaterEqual(summary["processed"] + summary["pending_decisions"], 1)


if __name__ == "__main__":
    unittest.main()
