# pyright: reportUnknownVariableType=false
"""The original filename is a STRONG hint — item F of docs/pre-prod-hardening.md.

"Never trust the filename" means the name must never *decide*. It does NOT mean
discarding it: the name stays a strong indicator, and it must weigh MORE the less
reliable the extracted content is. When the "content" is itself an AI's reading of
an image (vision/OCR), telling the model the content is authoritative is exactly
backwards.

These tests assert on the BUILT PROMPT, never on a live call — the suite is
offline. They can prove the hint reaches the model and how it is framed; they
cannot prove the model obeys. That part needs a real run (see the F notes in
docs/pre-prod-hardening.md).
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from procrafiler.ai_analysis import (
    MAX_SIBLING_CHARS,
    MAX_SIBLING_HINTS,
    _build_analysis_prompt,
    _build_hints_block,
)
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.pipeline import process_all_inbox_files, process_next_inbox_file


class TestHintFraming(unittest.TestCase):
    """F1 — the hint's authority must be a function of `read_via`."""

    COMMON = dict(original_filename="constat_degats_eaux.jpg", source_folder="Degats-eaux")

    def test_a_mechanical_read_keeps_the_content_authoritative(self) -> None:
        block = _build_hints_block(read_via="text", sibling_filenames=None, **self.COMMON)
        self.assertIn("the content is authoritative", block)
        self.assertNotIn("may be incomplete, misread or invented", block)

    def test_an_image_description_marks_the_content_unreliable(self) -> None:
        block = _build_hints_block(read_via="vision", sibling_filenames=None, **self.COMMON)
        self.assertIn("may be incomplete, misread or invented", block)
        self.assertIn("RELIABLE", block)
        # It must NOT still claim the content is authoritative.
        self.assertNotIn("the content is authoritative", block)

    def test_ocr_is_treated_as_a_reliable_read(self) -> None:
        """A dedicated OCR model transcribing a page is reliable in practice — text
        read off a document is still text. Only IMAGE DESCRIPTION is the weak source.
        Grouping OCR with vision would wrongly demote a trustworthy read and let a
        filename hint override a correctly transcribed scan."""
        block = _build_hints_block(read_via="ocr", sibling_filenames=None, **self.COMMON)
        self.assertIn("the content is authoritative", block)
        self.assertNotIn("may be incomplete, misread or invented", block)

    def test_an_ai_read_tells_the_model_what_to_do_on_a_contradiction(self) -> None:
        """The point of the whole item: a confident name beating a vague image."""
        block = _build_hints_block(read_via="vision", sibling_filenames=None, **self.COMMON)
        self.assertIn("prefer the evidence", block)
        self.assertIn("category_path null", block)

    def test_an_unknown_read_via_falls_back_to_the_cautious_framing(self) -> None:
        block = _build_hints_block(read_via=None, sibling_filenames=None, **self.COMMON)
        self.assertIn("the content is authoritative", block)

    def test_the_filename_always_reaches_the_prompt(self) -> None:
        for read_via in ("text", "ocr", "vision", None):
            with self.subTest(read_via=read_via):
                block = _build_hints_block(read_via=read_via, sibling_filenames=None, **self.COMMON)
                self.assertIn("constat_degats_eaux.jpg", block)

    def test_no_hints_at_all_yields_an_empty_block(self) -> None:
        self.assertEqual(
            _build_hints_block(
                original_filename=None, source_folder=None, sibling_filenames=None, read_via="vision"
            ),
            "",
        )

    def test_the_framing_reaches_the_full_analysis_prompt(self) -> None:
        """Wiring: the block is useless if `_build_analysis_prompt` drops it."""
        prompt = _build_analysis_prompt(
            "some extracted text",
            ["Personal"],
            [],
            "constat_degats_eaux.jpg",
            "Degats-eaux",
            None,
            "fr",
            sibling_filenames=["facture_plombier.pdf"],
            read_via="vision",
        )
        self.assertIn("may be incomplete, misread or invented", prompt)
        self.assertIn("constat_degats_eaux.jpg", prompt)
        self.assertIn("facture_plombier.pdf", prompt)


class TestSiblingHints(unittest.TestCase):
    """F2 — the names of the files dropped alongside are context, not noise."""

    def test_sibling_names_appear_in_the_block(self) -> None:
        block = _build_hints_block(
            original_filename="photo_2.jpg",
            source_folder="Degats-eaux",
            sibling_filenames=["facture_plombier.pdf", "constat_amiable.pdf"],
            read_via="vision",
        )
        self.assertIn("facture_plombier.pdf", block)
        self.assertIn("constat_amiable.pdf", block)
        self.assertIn("same set", block)

    def test_the_sibling_list_is_capped_by_count(self) -> None:
        names = [f"f{i}.pdf" for i in range(50)]
        block = _build_hints_block(
            original_filename="x.jpg", source_folder=None, sibling_filenames=names, read_via="text"
        )
        self.assertNotIn(f"f{MAX_SIBLING_HINTS + 1}.pdf", block)

    def test_the_sibling_list_is_capped_by_length(self) -> None:
        """A few very long names must not blow the prompt budget either."""
        names = ["x" * 150 + f"_{i}.pdf" for i in range(MAX_SIBLING_HINTS)]
        block = _build_hints_block(
            original_filename="x.jpg", source_folder=None, sibling_filenames=names, read_via="text"
        )
        self.assertLess(len(block), MAX_SIBLING_CHARS + 600)

    def test_no_siblings_omits_the_line_entirely(self) -> None:
        block = _build_hints_block(
            original_filename="x.jpg", source_folder=None, sibling_filenames=[], read_via="text"
        )
        self.assertNotIn("same set", block)


class TestSiblingsReachThePipeline(unittest.TestCase):
    """Wiring through the real pipeline — a hint nobody passes is no hint."""

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
        self.now = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
        self.tmp.cleanup()

    _REPLY = '{"name": "Doc", "category_path": "Personal/Misc", "summary": "s", "keywords": []}'

    def test_process_all_passes_the_whole_set_as_siblings(self) -> None:
        folder = self.paths.inbox_dir / "Degats-eaux"
        folder.mkdir(parents=True, exist_ok=True)
        for name in ("constat.txt", "facture_plombier.txt", "photo.txt"):
            (folder / name).write_text(f"content of {name}")

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._REPLY) as call:
            process_all_inbox_files(self.paths, now_utc=self.now)

        prompts = [c.args[0] for c in call.call_args_list]
        self.assertEqual(len(prompts), 3)
        # Every file must have seen the OTHER two named, and never itself.
        for prompt, own in zip(prompts, ("constat.txt", "facture_plombier.txt", "photo.txt")):
            others = {"constat.txt", "facture_plombier.txt", "photo.txt"} - {own}
            for other in others:
                self.assertIn(other, prompt, f"{own} did not see sibling {other}")
            self.assertIn("same set", prompt)

    def test_a_loose_inbox_root_file_gets_no_sibling_line(self) -> None:
        """Files loose at the root are singletons, not a set — no false context."""
        (self.paths.inbox_dir / "alone.txt").write_text("body")
        (self.paths.inbox_dir / "unrelated.txt").write_text("other body")

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._REPLY) as call:
            process_all_inbox_files(self.paths, now_utc=self.now)

        for c in call.call_args_list:
            self.assertNotIn("same set", c.args[0])

    def test_process_once_passes_the_siblings_still_in_the_folder(self) -> None:
        folder = self.paths.inbox_dir / "Degats-eaux"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "a_first.txt").write_text("first")
        (folder / "b_second.txt").write_text("second")

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._REPLY) as call:
            process_next_inbox_file(self.paths, now_utc=self.now)

        self.assertIn("b_second.txt", call.call_args_list[0].args[0])

    def test_a_vision_read_flips_the_framing_end_to_end(self) -> None:
        """THE case behind item F: a photo whose description came from a vision model.
        The whole chain must carry `read_via="vision"` into the prompt, so the
        filename and its set-mates become corroborating evidence rather than mere
        tie-breakers. This is the wiring test — the framing helper being right is
        worthless if `read_via` never reaches it."""
        folder = self.paths.inbox_dir / "Degats-eaux"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "constat_degats_eaux.jpg").write_bytes(b"\xff\xd8\xff\xe0 not-a-real-jpeg")
        (folder / "facture_plombier.txt").write_text("plumber invoice body")

        vision_reply = type(
            "R", (), {"text": "A blurry indoor photo.", "provider": "p", "model": "m", "reason": None, "is_document": False}
        )()
        with patch("procrafiler.pipeline.read_with_vision", return_value=vision_reply):
            with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._REPLY) as call:
                process_all_inbox_files(self.paths, now_utc=self.now)

        image_prompts = [c.args[0] for c in call.call_args_list if "blurry indoor photo" in c.args[0]]
        self.assertEqual(len(image_prompts), 1, "the vision-read image was never analysed")
        prompt = image_prompts[0]
        self.assertIn("may be incomplete, misread or invented", prompt)
        self.assertIn("RELIABLE", prompt)
        self.assertIn("prefer the evidence", prompt)
        # …and the reliable facts it should lean on are all there.
        self.assertIn("constat_degats_eaux.jpg", prompt)
        self.assertIn("Degats-eaux", prompt)
        self.assertIn("facture_plombier.txt", prompt)

    def test_a_text_read_is_declared_as_such_in_the_prompt(self) -> None:
        """End-to-end: a .txt file is read mechanically, so the prompt must use the
        authoritative-content framing, not the corroborating-evidence one."""
        (self.paths.inbox_dir / "doc.txt").write_text("a plain text body")

        with patch("procrafiler.ai_analysis.call_mistral_chat", return_value=self._REPLY) as call:
            process_next_inbox_file(self.paths, now_utc=self.now)

        prompt = call.call_args_list[0].args[0]
        self.assertIn("the content is authoritative", prompt)
        self.assertNotIn("may be incomplete, misread or invented", prompt)


if __name__ == "__main__":
    unittest.main()
