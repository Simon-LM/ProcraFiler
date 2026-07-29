"""Real, opt-in integration tests against the MISTRAL API — these COST money.

Skipped by default. They only run with `PROCRAFILER_MISTRAL_IT=1`::

    PROCRAFILER_MISTRAL_IT=1 .venv/bin/python -m unittest tests.test_mistral_integration
    # or: make test-mistral

**Why these exist.** Every other test in the suite mocks the AI, so they prove a
prompt is built and a verdict is applied — never that the model *judges well*.
The set-aware naming pass exists precisely to catch a photo whose vision reading
went wrong, and that judgement is the one thing no offline test can measure. These
tests are the only place it is measured.

**Why no photos are needed.** The naming pass never sees an image: it receives
`read_via`, the original filename, the name the per-file analysis proposed, and a
summary. A misread photo is an *input* to it. So the scenario reproduces exactly
by supplying what a vision model would have produced — a plausible wrong reading.

**How they assert.** On the DISCRIMINATION, never on exact strings and never on the
review flag. Observed variability across real runs: the same outlier came back as
`Degats-eaux_pelouse-jardin` in one run and `Degats-eaux_tapis-salon` in another,
separators drifted (`Chat-sur-canape` → `Chat_sur_canape`), and the review flag was
set on one run and not the next. All of that is irrelevant to the claim. What must
hold is: **a plausibly-misread photo joins its set, while a genuinely unrelated one
does not.** A test demanding an exact name would be red one run in three and end up
ignored — worse than no test.
"""

from __future__ import annotations

import os
import tempfile
import unicodedata
import unittest
from pathlib import Path

_ENABLED = os.environ.get("PROCRAFILER_MISTRAL_IT") == "1"

# The pass sees the whole set at once, so it wants a capable model.
NAMING_MODEL = "mistral:mistral-medium-latest"


def _load_real_env() -> None:
    """Load the real key, which the suite bootstrap deliberately hides.

    `tests/__init__.py` points PROCRAFILER_ENV_FILE at an empty file so the routine
    suite can never reach the API. These tests are the sanctioned exception, so they
    load the developer/user env explicitly rather than relying on that lookup.
    """
    from procrafiler.runtime_env import load_runtime_env

    config_home = Path(
        os.environ.get("PROCRAFILER_CONFIG_HOME", str(Path.home() / ".config" / "procrafiler"))
    )
    load_runtime_env([Path.cwd() / ".env", config_home / "procrafiler.env"])


def _normalize(text: str) -> str:
    """Casefolded, accent-free, separator-free — so `Dégats-eaux` and `degats_eaux`
    compare equal. The claim is about the theme surviving, not about punctuation."""
    stripped = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    return "".join(ch for ch in stripped.lower() if ch.isalnum())


@unittest.skipUnless(_ENABLED, "set PROCRAFILER_MISTRAL_IT=1 to run real Mistral API tests (costs money)")
class TestSetNamingJudgement(unittest.TestCase):
    """One API call per test: the pass is invoked once per dropped folder."""

    @classmethod
    def setUpClass(cls) -> None:
        _load_real_env()
        if not os.environ.get("MISTRAL_API_KEY", "").strip():
            raise unittest.SkipTest("MISTRAL_API_KEY is not set")

    def _name_set(self, folder: str, docs: list[dict], context: str) -> dict[int, str]:
        """Run the pass and return index -> final name (its own name when the model
        said nothing about it, which is the documented no-op)."""
        from procrafiler.ai_naming import parse_provider_chain
        from procrafiler.ai_set_naming import name_set

        result = name_set(
            docs,
            source_folder=folder,
            user_context=context,
            chain=parse_provider_chain(NAMING_MODEL),
        )
        self.assertFalse(
            result.used_fallback, f"the naming pass did not reach the model: {result.reason}"
        )
        names: dict[int, str] = {}
        for index, doc in enumerate(docs):
            verdict = result.names.get(index)
            names[index] = verdict.name if verdict and verdict.name else doc["proposed_name"]
        return names

    def assertJoinedTheSet(self, name: str, theme: str, label: str) -> None:
        self.assertIn(
            _normalize(theme), _normalize(name),
            f"{label}: a plausibly-misread photo was left outside its set (got {name!r})",
        )

    def assertStayedOut(self, name: str, theme: str, label: str) -> None:
        self.assertNotIn(
            _normalize(theme), _normalize(name),
            f"{label}: a genuinely unrelated file was absorbed into the set (got {name!r})",
        )

    # --- water damage: two plausible misreads, two controls -------------

    def test_water_damage_set_discriminates_misreads_from_strangers(self) -> None:
        folder, theme = "Degats-eaux-cuisine", "degatseaux"
        docs = [
            {"read_via": "vision", "original_filename": "IMG_001.jpg",
             "proposed_name": "Degats-eaux_mur-cuisine",
             "summary": "Mur de cuisine tache, peinture cloquee par l'humidite"},
            # A green fibrous close-up in a flooded kitchen is a soaked carpet, not a lawn.
            {"read_via": "vision", "original_filename": "IMG_002.jpg",
             "proposed_name": "Pelouse_jardin",
             "summary": "Gros plan sur une etendue verte fibreuse, semblable a du gazon, aspect detrempe"},
            # A mottled pale expanse overhead is a stained ceiling, not the sky.
            {"read_via": "vision", "original_filename": "IMG_003.jpg",
             "proposed_name": "Ciel-nuageux",
             "summary": "Grande surface claire mouchetee de taches grises et brunes, aspect nuageux"},
            # The cat is real but incidental — the subject is the soaked rug.
            {"read_via": "vision", "original_filename": "IMG_004.jpg",
             "proposed_name": "Chat-sur-tapis",
             "summary": "Un chat roux assis sur un tapis detrempe, eau stagnante autour"},
            # CONTROL: a cat on a sofa in a dry, tidy room is genuinely unrelated.
            {"read_via": "vision", "original_filename": "IMG_005.jpg",
             "proposed_name": "Chat-sur-canape",
             "summary": "Un chat roux sur un canape, piece seche et rangee, aucun degat visible"},
            {"read_via": "text", "original_filename": "devis.pdf",
             "proposed_name": "Devis_Plombier-Martin",
             "summary": "Devis de reparation plomberie, entreprise Martin, 1240 EUR"},
        ]
        names = self._name_set(folder, docs, "Simon, developpeur web. Proprietaire de son logement.")

        self.assertJoinedTheSet(names[1], theme, "lawn/carpet confusion")
        self.assertJoinedTheSet(names[2], theme, "sky/ceiling confusion")
        self.assertJoinedTheSet(names[3], theme, "cat on a soaked rug")
        self.assertStayedOut(names[4], theme, "cat on a dry sofa (control)")
        # A reliably-read document keeps its own identity; it is not dissolved
        # into the theme.
        self.assertIn("devis", _normalize(names[5]), f"the quote lost its identity: {names[5]!r}")

    # --- a different domain: the principle must be generalist ------------

    def test_the_same_judgement_works_outside_water_damage(self) -> None:
        """Nothing in the prompt is water-damage specific. Crumpled bodywork read as
        an abstract sculpture must be recontextualised in a car-accident folder,
        while an unrelated meal photo must not."""
        folder, theme = "Accident-voiture-mars-2026", "accident"
        docs = [
            {"read_via": "vision", "original_filename": "IMG_101.jpg",
             "proposed_name": "Accident_aile-avant",
             "summary": "Aile avant droite enfoncee, phare brise"},
            {"read_via": "vision", "original_filename": "IMG_102.jpg",
             "proposed_name": "Sculpture-metallique-abstraite",
             "summary": "Forme metallique tordue et froissee, reflets, texture irreguliere"},
            # CONTROL: taken the same day, but nothing to do with the claim.
            {"read_via": "vision", "original_filename": "IMG_103.jpg",
             "proposed_name": "Repas-restaurant",
             "summary": "Assiette de pates sur une table de restaurant"},
            {"read_via": "text", "original_filename": "constat.pdf",
             "proposed_name": "Constat-amiable_Dupont",
             "summary": "Constat amiable, tiers M. Dupont, carrefour rue des Lilas"},
        ]
        names = self._name_set(folder, docs, "Simon, developpeur web. Possede une voiture.")

        self.assertJoinedTheSet(names[1], theme, "sculpture/bodywork confusion")
        self.assertStayedOut(names[2], theme, "restaurant meal (control)")


@unittest.skipUnless(_ENABLED, "set PROCRAFILER_MISTRAL_IT=1 to run real Mistral API tests (costs money)")
class TestPhotographedDocumentIsRecognised(unittest.TestCase):
    """Does the vision model actually answer the `DOCUMENT: oui|non` question?

    Offline tests can only prove the marker is parsed and the OCR re-read is wired.
    Whether the model classifies a photo correctly is measurable only for real.
    Two images, two calls.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _load_real_env()
        if not os.environ.get("MISTRAL_API_KEY", "").strip():
            raise unittest.SkipTest("MISTRAL_API_KEY is not set")
        os.environ["PROCRAFILER_AI_IMAGE_PRIMARY"] = "mistral:mistral-medium-latest"
        os.environ["PROCRAFILER_AI_OCR_PRIMARY"] = "mistral:mistral-ocr-latest"
        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _document_image(self) -> Path:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (900, 420), "white")
        draw = ImageDraw.Draw(img)
        for i, line in enumerate([
            "FACTURE  N 2026-0412", "EDF - Electricite", "Client : Simon L.",
            "Periode : mars 2026", "Montant TTC : 87,40 EUR",
        ]):
            draw.text((40, 40 + i * 60), line, fill="black")
        path = self._dir / "photo_facture.jpg"
        img.save(path)
        return path

    def _scene_image(self) -> Path:
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (900, 420), (90, 140, 80))
        ImageDraw.Draw(img).ellipse((300, 150, 600, 350), fill=(200, 120, 60))
        path = self._dir / "photo_scene.jpg"
        img.save(path)
        return path

    def test_a_photographed_document_is_flagged_and_transcribed_by_ocr(self) -> None:
        from procrafiler.ai_reader import read_with_ocr, read_with_vision

        result = read_with_vision(self._document_image())
        self.assertTrue(result.is_document, "the vision model did not recognise a written document")
        self.assertNotIn("DOCUMENT", result.text or "", "the marker leaked into the cached text")

        ocr = read_with_ocr(self._document_image())
        self.assertIsNotNone(ocr.text)
        # The point of the re-read: the figures come back, not a description.
        self.assertIn("2026-0412", ocr.text or "")
        self.assertIn("87,40", ocr.text or "")

    def test_a_plain_scene_is_not_flagged_as_a_document(self) -> None:
        """The control: a photo with no text must not cost a second, useless call."""
        from procrafiler.ai_reader import read_with_vision

        result = read_with_vision(self._scene_image())
        self.assertFalse(result.is_document, "a textless scene was taken for a document")


@unittest.skipUnless(_ENABLED, "set PROCRAFILER_MISTRAL_IT=1 to run real Mistral API tests (costs money)")
class TestVisionNameHints(unittest.TestCase):
    """Does naming the file to the vision model help — or does it just contaminate?

    That question is the entire reason this was deferred for so long, and it is not
    answerable offline: offline tests prove the names are SENT, never what the model
    does with them. Both halves are measured here, on one deliberately ambiguous
    image plus one that is not ambiguous at all.
    """

    @classmethod
    def setUpClass(cls) -> None:
        _load_real_env()
        if not os.environ.get("MISTRAL_API_KEY", "").strip():
            raise unittest.SkipTest("MISTRAL_API_KEY is not set")
        os.environ["PROCRAFILER_AI_IMAGE_PRIMARY"] = "mistral:mistral-medium-latest"
        cls._tmp = tempfile.TemporaryDirectory()
        cls._dir = Path(cls._tmp.name)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._tmp.cleanup()

    def _green_texture(self) -> Path:
        """A close-up of green fibres at no discernible scale — the user's own case.

        Lawn, or a soaked carpet? The image genuinely cannot say, which is what
        makes it the right probe: any difference in the reading comes from the
        hint, because there is nothing else to come from.
        """
        import random

        from PIL import Image

        rng = random.Random(20260729)
        img = Image.new("RGB", (600, 600))
        img.putdata([
            (rng.randint(30, 90), rng.randint(90, 170), rng.randint(30, 80))
            for _ in range(600 * 600)
        ])
        path = self._dir / "texture.jpg"
        img.save(path)
        return path

    def _scene_image(self) -> Path:
        """Unambiguous on purpose: an orange disc on green. Nothing about it can
        honestly be read as a written document."""
        from PIL import Image, ImageDraw

        img = Image.new("RGB", (900, 420), (90, 140, 80))
        ImageDraw.Draw(img).ellipse((300, 150, 600, 350), fill=(200, 120, 60))
        path = self._dir / "scene.jpg"
        img.save(path)
        return path

    def _read(self, path: Path, filename: str | None, folder: str | None) -> str:
        from procrafiler.ai_reader import read_with_vision

        result = read_with_vision(path, original_filename=filename, source_folder=folder)
        self.assertIsNotNone(result.text, f"the vision read failed: {result.reason}")
        return _normalize(result.text or "")

    def assertMentionsOne(self, text: str, tokens: tuple[str, ...], label: str) -> None:
        self.assertTrue(
            any(token in text for token in tokens),
            f"{label}: none of {tokens} in the reading — the hint did not land.\n{text[:400]}",
        )

    def assertMentionsNone(self, text: str, tokens: tuple[str, ...], label: str) -> None:
        found = [token for token in tokens if token in text]
        self.assertFalse(
            found,
            f"{label}: the reading did not commit — it still mentions {found}.\n{text[:400]}",
        )

    # Words that COMMIT the reading to one subject. Kept tight, so each set can also
    # serve as the other's exclusion list.
    CARPET = ("tapis", "moquette", "interieur", "salon")
    LAWN = ("pelouse", "gazon", "herbe", "jardin", "exterieur")
    # Accepted as evidence of the water-damage context, but never as exclusion:
    # a garden reading may legitimately call wet grass "humide".
    SOAKED = ("humid", "detremp", "inond")

    def test_the_same_ambiguous_image_reads_differently_under_each_context(self) -> None:
        """The benefit half. Identical pixels, two drop folders, two readings.

        Asserted on French stems only, and on a SET of acceptable ones: the claim is
        that the context reached the model, not that it chose a particular word.

        **Why the assertion is exclusive.** Measured 2026-07-29, mistral-medium-latest,
        this same JPEG read with NO hint came back "un fond ou un motif abstrait" on
        one run and "de l'herbe ou un tissu" on the next — unhinted the model either
        abstains or offers both, and which one is not stable enough to assert on. What
        IS stable is that a hint makes it commit: naming one subject *and dropping the
        other*. Measured, same image:
            Degats-eaux → "ressemblant à un tapis ou une moquette"
            Jardin      → "ressemblant à une pelouse bien tondue ou à un gazon dense"
        Neither reading mentions the rival subject. That exclusivity cannot come from
        the pixels — they are identical — so it is the hint, and no third call is
        needed to establish it.
        """
        image = self._green_texture()

        indoors = self._read(image, "tapis-detrempe.jpg", "Degats-eaux-salon")
        self.assertMentionsOne(indoors, self.CARPET + self.SOAKED, "water-damage context")
        self.assertMentionsNone(indoors, self.LAWN, "water-damage context")

        outdoors = self._read(image, "pelouse-tondue.jpg", "Jardin-printemps")
        self.assertMentionsOne(outdoors, self.LAWN, "garden context")
        self.assertMentionsNone(outdoors, self.CARPET, "garden context")

    def test_a_wrong_name_does_not_turn_a_scene_into_a_document(self) -> None:
        """The risk half — the objection that kept this feature shelved.

        A photo of a garden misnamed `facture-EDF-mars-2026.jpg`, sitting in a folder
        of invoices. If the model agrees with the name, it reports an invoice that
        does not exist, the marker fires, and a pointless OCR call follows. Every
        later pass would then be reasoning about a document nobody photographed.
        """
        from procrafiler.ai_reader import read_with_vision

        result = read_with_vision(
            self._scene_image(),
            original_filename="facture-EDF-mars-2026.jpg",
            source_folder="Factures-2026",
        )
        self.assertIsNotNone(result.text, f"the vision read failed: {result.reason}")
        self.assertFalse(
            result.is_document,
            f"a misleading name turned a textless scene into a document: {result.text!r}",
        )
        text = _normalize(result.text or "")
        for invented in ("facture", "edf", "montant", "euro"):
            self.assertNotIn(
                invented, text, f"the reading invented {invented!r} from the filename"
            )


if __name__ == "__main__":
    unittest.main()
