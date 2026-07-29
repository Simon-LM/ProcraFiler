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


if __name__ == "__main__":
    unittest.main()
