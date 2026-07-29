# pyright: reportUnknownVariableType=false
"""Nothing personal may reach the public repository.

`sandbox/samples/` is the one tracked directory that invites documents: it is
where the fixtures a run consumes live, so it is the natural place to drop "just
one real invoice to see what happens". That file would go to GitHub.

The history was audited on 2026-07-29 and is clean — five files have ever existed
under `sandbox/`, and no PDF, image or office document was ever committed anywhere
in the project. This test is prevention, not remediation: real material belongs in
`private/`, which is ignored.
"""
from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLES = REPO_ROOT / "sandbox" / "samples"

# The invented fixtures the sandbox runs on. Adding a genuinely synthetic one here
# is a normal change; adding a real document is what this test exists to stop.
KNOWN_SAMPLES = {
    "facture-edf.txt",
    "note-perso.txt",
    "releve-bancaire.txt",
}

# Formats a real document arrives in. A sample is a plain-text stand-in, so any of
# these appearing under a tracked path is either a real document or a binary that
# has no business in the repository.
DOCUMENT_SUFFIXES = {
    ".pdf", ".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif", ".tif", ".tiff",
    ".doc", ".docx", ".odt", ".xls", ".xlsx", ".ods", ".ppt", ".pptx", ".eml", ".msg",
}


class TestSandboxSamplesStayFake(unittest.TestCase):
    def test_only_the_known_text_fixtures_are_present(self) -> None:
        self.assertTrue(SAMPLES.is_dir(), f"{SAMPLES} is missing")
        present = {p.name for p in SAMPLES.iterdir() if p.is_file()}
        unexpected = present - KNOWN_SAMPLES
        self.assertFalse(
            unexpected,
            f"unexpected files in a TRACKED directory: {sorted(unexpected)}.\n"
            "If these are real documents they would be pushed to GitHub — move them "
            "to private/ (gitignored). If they are genuinely invented fixtures, add "
            "their names to KNOWN_SAMPLES.",
        )

    def test_no_sample_is_a_real_document_format(self) -> None:
        """Belt and braces: even a name added to KNOWN_SAMPLES cannot be a PDF."""
        for path in SAMPLES.iterdir():
            if path.is_file():
                self.assertNotIn(
                    path.suffix.lower(), DOCUMENT_SUFFIXES,
                    f"{path.name} is a document format, not a text fixture",
                )


class TestPrivateMaterialIsIgnored(unittest.TestCase):
    def test_the_private_directory_is_gitignored(self) -> None:
        """`private/` is where real personal material goes. If the ignore rule is
        ever dropped, the next `git add -A` publishes it."""
        gitignore = REPO_ROOT / ".gitignore"
        self.assertTrue(gitignore.is_file())
        rules = {
            line.strip()
            for line in gitignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        self.assertIn("private/", rules, "private/ is no longer ignored by git")

    def test_the_sandbox_script_claims_the_workspace_as_a_sandbox(self) -> None:
        """`sandbox/run.sh` writes the marker the dev guard looks for. A workspace
        that predates the guard already holds test documents and carries no marker,
        so without this line the guard refuses the sandbox as "a library that
        already holds documents" — which is how this was found."""
        from procrafiler.dev_guard import SANDBOX_MARKER_NAME

        script = (REPO_ROOT / "sandbox" / "run.sh").read_text(encoding="utf-8")
        self.assertIn(
            SANDBOX_MARKER_NAME, script,
            "sandbox/run.sh no longer writes the marker the guard expects — "
            "the two have drifted apart",
        )

    def test_the_sandbox_workspace_is_gitignored(self) -> None:
        """Where a sandbox run actually files documents — including any real one the
        developer dropped into the inbox to try it out."""
        gitignore = REPO_ROOT / ".gitignore"
        rules = {
            line.strip()
            for line in gitignore.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        self.assertIn("sandbox/workspace/", rules)


if __name__ == "__main__":
    unittest.main()
