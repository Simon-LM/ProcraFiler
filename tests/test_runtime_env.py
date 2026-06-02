from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.runtime_env import load_runtime_env


class TestRuntimeEnv(unittest.TestCase):
    def test_load_runtime_env_from_candidate_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "procrafiler.env"
            env_file.write_text(
                "# comment\n"
                "PROCRAFILER_AI_ANALYSIS_PRIMARY=mistral:mistral-small-2506\n"
                "MISTRAL_API_KEY=abc123\n",
                encoding="utf-8",
            )

            os.environ.pop("PROCRAFILER_AI_ANALYSIS_PRIMARY", None)
            os.environ.pop("MISTRAL_API_KEY", None)
            os.environ.pop("PROCRAFILER_ENV_LOADED_FROM", None)

            loaded = load_runtime_env([env_file])

            self.assertEqual(loaded, env_file)
            self.assertEqual(os.environ.get("PROCRAFILER_AI_ANALYSIS_PRIMARY"), "mistral:mistral-small-2506")
            self.assertEqual(os.environ.get("MISTRAL_API_KEY"), "abc123")
            self.assertEqual(os.environ.get("PROCRAFILER_ENV_LOADED_FROM"), str(env_file))

    def test_does_not_override_existing_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "procrafiler.env"
            env_file.write_text("MISTRAL_API_KEY=file_value\n", encoding="utf-8")

            os.environ["MISTRAL_API_KEY"] = "existing_value"
            load_runtime_env([env_file])

            self.assertEqual(os.environ.get("MISTRAL_API_KEY"), "existing_value")


if __name__ == "__main__":
    unittest.main()
