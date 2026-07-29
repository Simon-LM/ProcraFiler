# pyright: reportUnknownVariableType=false
"""The vision reader is told the file's own name and the folder it was dropped in.

A vision model reads pixels with no idea what it is looking at. The green fibrous
close-up that started this whole thread is a lawn in a holiday folder and a soaked
carpet in a water-damage one, and NOTHING in the image settles it — so the read
comes back wrong before any later pass gets a chance to weigh it.

The obvious objection is why this was deferred for so long: naming the file risks
the model simply agreeing with the name, contaminating the one signal we wanted
independent of it. So the hint is granted exactly one power — breaking a tie — and
denied the power to add content. These tests pin that boundary as far as offline
tests can reach: they prove WHAT is sent. Whether the model obeys the caveat is
measured for real in `tests/test_mistral_integration.py`.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import procrafiler.pipeline as pipeline
from procrafiler.ai_reader import (
    _DEFAULT_VISION_PROMPT,
    _VISION_DOCUMENT_QUESTION,
    build_vision_prompt,
    read_with_vision,
)
from procrafiler.config import default_runtime_paths, ensure_runtime_layout


class TestVisionPromptHints(unittest.TestCase):
    def test_no_names_leaves_the_prompt_untouched(self) -> None:
        """An empty hint block would be worse than none: it invites the model to
        fill it in."""
        self.assertEqual(build_vision_prompt(), _DEFAULT_VISION_PROMPT)
        self.assertNotIn("Indices de provenance", build_vision_prompt())

    def test_the_filename_is_shown(self) -> None:
        prompt = build_vision_prompt(original_filename="facture-EDF-mars.jpg")
        self.assertIn("facture-EDF-mars.jpg", prompt)

    def test_the_source_folder_is_shown(self) -> None:
        prompt = build_vision_prompt(source_folder="Degats-eaux-cuisine")
        self.assertIn("Degats-eaux-cuisine", prompt)

    def test_each_name_is_omitted_on_its_own(self) -> None:
        """A file at the Inbox root has no folder; a folder can be dropped with
        files whose names are blank after stripping. Neither may emit a dangling
        bullet the model has to interpret."""
        only_file = build_vision_prompt(original_filename="a.jpg", source_folder="   ")
        self.assertIn("nom du fichier", only_file)
        self.assertNotIn("dossier d'origine", only_file)

        only_folder = build_vision_prompt(original_filename="", source_folder="Voyage")
        self.assertIn("dossier d'origine", only_folder)
        self.assertNotIn("nom du fichier", only_folder)

    def test_the_names_are_declared_fallible(self) -> None:
        """Without this the hint is an instruction, not a clue — and a photo named
        `facture.jpg` comes back as an invoice whatever it shows."""
        prompt = build_vision_prompt(original_filename="x.jpg", source_folder="y")
        self.assertIn("ne font pas foi", prompt)
        self.assertIn("Décris uniquement ce que l'image montre", prompt)
        self.assertIn("contredis-les", prompt)
        # …and the one thing they ARE allowed to do.
        self.assertIn("départager", prompt)

    def test_the_document_question_stays_the_last_instruction(self) -> None:
        """It asks for a FINAL line. Appending the hint block after it would quietly
        break the `DOCUMENT: oui|non` marker, and with it the OCR re-read."""
        for kwargs in (
            {},
            {"original_filename": "x.jpg"},
            {"source_folder": "y"},
            {"original_filename": "x.jpg", "source_folder": "y"},
        ):
            with self.subTest(**kwargs):
                prompt = build_vision_prompt(**kwargs)
                self.assertTrue(
                    prompt.rstrip().endswith(_VISION_DOCUMENT_QUESTION),
                    f"the DOCUMENT question is no longer last for {kwargs}",
                )

    def test_the_built_prompt_actually_reaches_the_provider(self) -> None:
        """The prompt can be perfect and still never be sent."""
        from procrafiler.ai_naming import ChainEntry

        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "queued-name-says-nothing.jpg"
            image.write_bytes(b"\xff\xd8\xff\xe0")
            with patch(
                "procrafiler.ai_reader.call_mistral_vision", return_value="desc\nDOCUMENT: non"
            ) as call:
                read_with_vision(
                    image,
                    chain=[ChainEntry("mistral", "m")],
                    original_filename="IMG_2024.jpg",
                    source_folder="Degats-eaux",
                )
        sent = call.call_args.kwargs["prompt"]
        self.assertIn("IMG_2024.jpg", sent)
        self.assertIn("Degats-eaux", sent)


class TestVisionHintsInPipeline(unittest.TestCase):
    """Which names the pipeline chooses to send — the part a prompt test cannot see."""

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
        self.now = datetime(2026, 7, 29, 12, 0, 0, tzinfo=timezone.utc)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _drop(self, relative: str) -> None:
        target = self.paths.inbox_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"\xff\xd8\xff\xe0 not-a-real-jpeg")

    def _reads(self):
        """Run the inbox and return the kwargs of every vision read."""
        reply = type(
            "R", (), {"text": "une surface verte", "provider": "p", "model": "m",
                      "reason": None, "used_fallback": False, "is_document": False}
        )()
        with patch.object(pipeline, "read_with_vision", return_value=reply) as reader:
            pipeline.process_all_inbox_files(self.paths, now_utc=self.now)
        self.assertTrue(reader.call_args_list, "no photo was read — the test proves nothing")
        return reader.call_args_list

    def test_a_photo_in_a_folder_carries_both_names(self) -> None:
        self._drop("Degats-eaux-cuisine/IMG_2024.jpg")
        call = self._reads()[0]
        self.assertEqual(call.kwargs["original_filename"], "IMG_2024.jpg")
        self.assertEqual(call.kwargs["source_folder"], "Degats-eaux-cuisine")

    def test_the_folder_cannot_have_come_from_the_read_path(self) -> None:
        """The file is read from the flat Queue, so its parent directory is
        `Queue` — the drop folder survives only because it is passed explicitly.
        This is the assertion that would catch someone "simplifying" the call to
        derive both names from the path."""
        self._drop("Degats-eaux-cuisine/IMG_2024.jpg")
        call = self._reads()[0]
        read_path = Path(call.args[0])
        self.assertEqual(read_path.parent, self.paths.queue_dir)
        self.assertNotEqual(call.kwargs["source_folder"], read_path.parent.name)

    def test_a_nested_drop_keeps_the_whole_relative_path(self) -> None:
        """`Degats-eaux/salon` says more than `salon`: the affair AND the room."""
        self._drop("Degats-eaux/salon/IMG_7.jpg")
        self.assertEqual(self._reads()[0].kwargs["source_folder"], "Degats-eaux/salon")

    def test_a_photo_at_the_inbox_root_has_no_folder(self) -> None:
        """Not the empty string: `build_vision_prompt` must see None and drop the
        line entirely."""
        self._drop("IMG_0001.jpg")
        call = self._reads()[0]
        self.assertIsNone(call.kwargs["source_folder"])
        self.assertEqual(call.kwargs["original_filename"], "IMG_0001.jpg")

    def test_the_name_sent_is_the_users_even_when_the_queue_renamed_it(self) -> None:
        """Same filename twice in one set: the whole set is catalogued before
        anything is filed, and the Queue is flat, so the second copy necessarily
        lands there under a disambiguated name. The model must still be told the
        name the user actually gave — the suffix is our bookkeeping, not a clue.

        This is the only place the two names can diverge, which makes it the only
        test that can catch the call being "simplified" to `queued_target.name`."""
        self._drop("Degats-eaux/IMG_2024.jpg")
        # Different bytes, or the second file is discarded as a duplicate.
        second = self.paths.inbox_dir / "Degats-eaux" / "salon" / "IMG_2024.jpg"
        second.parent.mkdir(parents=True, exist_ok=True)
        second.write_bytes(b"\xff\xd8\xff\xe0 a different photo")

        calls = self._reads()
        self.assertEqual(len(calls), 2, "both photos should have been read")
        queued = sorted(Path(c.args[0]).name for c in calls)
        self.assertNotEqual(
            queued[0], queued[1], "the Queue did not disambiguate — the test proves nothing"
        )
        self.assertEqual(
            [c.kwargs["original_filename"] for c in calls], ["IMG_2024.jpg"] * 2
        )
        self.assertEqual(
            sorted(c.kwargs["source_folder"] for c in calls),
            ["Degats-eaux", "Degats-eaux/salon"],
        )


if __name__ == "__main__":
    unittest.main()
