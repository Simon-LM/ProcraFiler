# pyright: reportUnknownVariableType=false
from __future__ import annotations

import fcntl
import io
import os
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from procrafiler.cli import main
from procrafiler.config import default_runtime_paths, ensure_runtime_layout
from procrafiler.doctor import (
    check_pricing,
    STATUS_FAIL,
    STATUS_OK,
    STATUS_SKIP,
    STATUS_WARN,
    check_ai_config,
    check_catalog,
    check_env,
    check_paths,
    check_runtime_lock,
    format_report,
    overall_exit_code,
    run_doctor,
)
from procrafiler.runtime_lock import LOCK_FILENAME


class TestDoctor(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        os.environ["PROCRAFILER_WORKSPACE_DIR"] = str(root / "ProcraFiler_Inbox")
        os.environ["PROCRAFILER_LIBRARY_DIR"] = str(root / "ProcraFiler_Library")
        os.environ["PROCRAFILER_LIBRARY_MIRROR_DIR"] = str(root / "ProcraFiler_Library_Mirror")
        os.environ["PROCRAFILER_HOME"] = str(root / ".state")
        os.environ["PROCRAFILER_CONFIG_HOME"] = str(root / ".config")
        # These tests assert on the AI config the doctor sees, so start from a
        # clean slate: drop any AI chain / key left in the environment by an
        # earlier test (e.g. one that loaded the repo .env via the CLI). Each
        # test sets the specific chain/key it needs.
        for key in [k for k in os.environ if k.startswith("PROCRAFILER_AI_")]:
            os.environ.pop(key, None)
        os.environ.pop("MISTRAL_API_KEY", None)
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)

    def tearDown(self) -> None:
        for key in (
            "PROCRAFILER_AI_ANALYSIS_PRIMARY",
            "PROCRAFILER_AI_ANALYSIS_FALLBACK",
            "PROCRAFILER_ENV_LOADED_FROM",
            "MISTRAL_API_KEY",
        ):
            os.environ.pop(key, None)
        self.tmp.cleanup()

    def test_paths_all_ok_on_fresh_layout(self) -> None:
        for check in check_paths(self.paths):
            self.assertEqual(check.status, STATUS_OK, f"{check.name}: {check.message}")

    def test_paths_fail_when_directory_missing(self) -> None:
        import shutil

        shutil.rmtree(self.paths.library_root)
        results = {c.name: c for c in check_paths(self.paths)}
        self.assertEqual(results["library_root"].status, STATUS_FAIL)
        self.assertIn("missing", results["library_root"].message)

    def test_env_warns_when_no_env_file_loaded(self) -> None:
        os.environ.pop("PROCRAFILER_ENV_LOADED_FROM", None)
        with mock.patch.dict(os.environ):
            # No explicit file was asked for — nothing was configured, which is a
            # WARN. (The suite bootstrap sets PROCRAFILER_ENV_FILE for the whole
            # run, so it has to be cleared to reach this branch.)
            os.environ.pop("PROCRAFILER_ENV_FILE", None)
            results = check_env(self.paths)
        self.assertEqual(results[0].status, STATUS_WARN)
        self.assertIn("no env file loaded", results[0].message)

    def test_env_fails_when_an_explicit_env_file_could_not_be_read(self) -> None:
        """Naming a file the app then cannot read is a FAIL, not a shrug: the run
        is silently using built-in defaults instead of the configuration asked
        for — a typo'd path, or a permission problem."""
        os.environ.pop("PROCRAFILER_ENV_LOADED_FROM", None)
        with mock.patch.dict(os.environ):
            os.environ["PROCRAFILER_ENV_FILE"] = "/nonexistent/typo/procrafiler.env"
            results = check_env(self.paths)
        self.assertEqual(results[0].status, STATUS_FAIL)
        self.assertIn("/nonexistent/typo/procrafiler.env", results[0].message)

    def test_env_ok_with_loaded_file_and_strict_permissions(self) -> None:
        env_file = self.paths.state_root / "procrafiler.env"
        env_file.write_text("X=1\n", encoding="utf-8")
        os.chmod(env_file, 0o600)
        os.environ["PROCRAFILER_ENV_LOADED_FROM"] = str(env_file)

        results = {c.name: c for c in check_env(self.paths)}
        self.assertEqual(results["env_file_loaded"].status, STATUS_OK)
        self.assertEqual(results["env_file_permissions"].status, STATUS_OK)

    def test_env_warns_on_loose_permissions(self) -> None:
        env_file = self.paths.state_root / "procrafiler.env"
        env_file.write_text("X=1\n", encoding="utf-8")
        os.chmod(env_file, 0o644)
        os.environ["PROCRAFILER_ENV_LOADED_FROM"] = str(env_file)

        results = {c.name: c for c in check_env(self.paths)}
        self.assertEqual(results["env_file_permissions"].status, STATUS_WARN)
        self.assertIn("too permissive", results["env_file_permissions"].message)

    def test_ai_config_warns_when_no_chain_configured(self) -> None:
        results = {c.name: c for c in check_ai_config()}
        self.assertEqual(results["task_analysis"].status, STATUS_WARN)
        self.assertEqual(results["mistral_api_key"].status, STATUS_SKIP)

    def test_ai_config_fails_when_mistral_used_without_api_key(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-2506"
        os.environ.pop("MISTRAL_API_KEY", None)

        results = {c.name: c for c in check_ai_config()}
        self.assertEqual(results["task_analysis"].status, STATUS_OK)
        self.assertEqual(results["mistral_api_key"].status, STATUS_FAIL)

    def test_ai_config_ok_when_mistral_used_with_api_key(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-2506"
        os.environ["MISTRAL_API_KEY"] = "test-key"

        results = {c.name: c for c in check_ai_config()}
        self.assertEqual(results["mistral_api_key"].status, STATUS_OK)

    def test_catalog_warns_when_db_empty(self) -> None:
        # ensure_runtime_layout already touched the file but it has size 0.
        results = check_catalog(self.paths)
        self.assertEqual(results[0].status, STATUS_WARN)
        self.assertIn("missing or empty", results[0].message)

    def test_catalog_ok_after_schema_init(self) -> None:
        from procrafiler.catalog import CatalogRepository

        repo = CatalogRepository(self.paths.catalog_db_file)
        repo.init_schema()

        results = {c.name: c for c in check_catalog(self.paths)}
        self.assertEqual(results["catalog_db"].status, STATUS_OK)
        self.assertEqual(results["catalog_schema"].status, STATUS_OK)
        self.assertIn("flow_state", results["catalog_schema"].message)

    def test_catalog_warns_when_flow_state_missing(self) -> None:
        # Simulate a pre-P1b database without the flow_state column.
        self.paths.catalog_db_file.unlink(missing_ok=True)
        with sqlite3.connect(self.paths.catalog_db_file) as conn:
            conn.execute(
                """
                CREATE TABLE documents (
                    doc_id TEXT PRIMARY KEY,
                    sha256 TEXT NOT NULL,
                    current_filename TEXT NOT NULL,
                    current_path TEXT NOT NULL,
                    status TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL
                )
                """
            )
            conn.commit()

        results = {c.name: c for c in check_catalog(self.paths)}
        self.assertEqual(results["catalog_db"].status, STATUS_OK)
        self.assertEqual(results["catalog_schema"].status, STATUS_WARN)
        self.assertIn("flow_state", results["catalog_schema"].message)

    def test_runtime_lock_ok_when_available(self) -> None:
        results = check_runtime_lock(self.paths)
        self.assertEqual(results[0].status, STATUS_OK)

    def test_runtime_lock_warns_when_held_by_another_process(self) -> None:
        lock_path = self.paths.state_root / LOCK_FILENAME
        external_fd = os.open(str(lock_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(external_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            results = check_runtime_lock(self.paths)
            self.assertEqual(results[0].status, STATUS_WARN)
            self.assertIn("held by another process", results[0].message)
        finally:
            fcntl.flock(external_fd, fcntl.LOCK_UN)
            os.close(external_fd)

    def test_overall_exit_code_zero_when_no_fail(self) -> None:
        # A healthy run has actually loaded its env file; the suite bootstrap names
        # one (tests/empty.env) but nothing in this test called load_runtime_env,
        # so record the successful load the way the CLI would.
        os.environ["PROCRAFILER_ENV_LOADED_FROM"] = os.environ["PROCRAFILER_ENV_FILE"]
        checks = run_doctor(self.paths)
        self.assertNotIn(STATUS_FAIL, [c.status for c in checks])
        self.assertEqual(overall_exit_code(checks), 0)

    def test_overall_exit_code_nonzero_on_any_fail(self) -> None:
        import shutil

        shutil.rmtree(self.paths.library_root)
        checks = run_doctor(self.paths)
        self.assertEqual(overall_exit_code(checks), 1)

    def test_format_report_groups_by_section_and_includes_summary(self) -> None:
        checks = run_doctor(self.paths)
        report = format_report(checks)
        for section in ("Paths", "Env", "AI", "Catalog", "Concurrency", "Summary"):
            self.assertIn(section, report)
        self.assertIn(f"{len(checks)} checks", report)

    def test_cli_doctor_command_returns_code_and_prints_report(self) -> None:
        stdout = io.StringIO()
        with redirect_stdout(stdout):
            code = main(["doctor"])
        self.assertEqual(code, 0)
        self.assertIn("ProcraFiler doctor", stdout.getvalue())
        self.assertIn("Summary", stdout.getvalue())


class TestDoctorPricing(unittest.TestCase):
    """Whether the models this installation calls can be priced at all.

    The answer changes without anyone touching ProcraFiler: the published table
    keys each entry by whatever the seller's page calls it, so a renamed label
    costs a price on the next weekly refresh. The refresh is deliberately NOT
    tightened to reject such a table — that would stop every price update, for
    every model, the day one label moved — so this is where the loss is made loud.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self._snapshot = {k: v for k, v in os.environ.items() if k.startswith("PROCRAFILER_")}
        for key in list(os.environ):
            if key.startswith("PROCRAFILER_"):
                del os.environ[key]
        self.addCleanup(lambda: os.environ.update(self._snapshot))
        for name, sub in (("WORKSPACE_DIR", "Inbox"), ("LIBRARY_DIR", "Library"),
                          ("LIBRARY_MIRROR_DIR", "Mirror"), ("HOME", "state"),
                          ("CONFIG_HOME", "config")):
            os.environ[f"PROCRAFILER_{name}"] = str(root / sub)
        self.paths = default_runtime_paths()
        ensure_runtime_layout(self.paths)
        self.config = self.paths.settings_file.parent

    def _statuses(self) -> dict[str, tuple[str, str]]:
        return {c.name: (c.status, c.message) for c in check_pricing(self.paths)}

    def test_a_priced_model_is_reported_with_the_label_it_used(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:mistral-small-latest"
        status, detail = self._statuses()["price_analysis"]
        self.assertEqual(status, STATUS_OK)
        self.assertIn('as "mistral small 4"', detail)

    def test_a_model_with_no_price_warns_and_says_how_to_fix_it(self) -> None:
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:no-such-model-anywhere"
        status, detail = self._statuses()["price_analysis"]
        self.assertEqual(status, STATUS_WARN)
        self.assertIn("no-such-model-anywhere", detail)
        self.assertIn("price_labels.json", detail, "a warning with no remedy is a dead end")

    def test_it_warns_when_the_rate_is_no_longer_published(self) -> None:
        os.environ["PROCRAFILER_AI_TRANSCRIBE_PRIMARY"] = "mistral:voxtral-mini-latest"
        status, detail = self._statuses()["price_transcribe"]
        self.assertEqual(status, STATUS_WARN)
        self.assertIn("2026-08-17", detail)
        self.assertIn("not a current one", detail)

    def test_a_local_model_is_skipped_rather_than_warned_about(self) -> None:
        """Anti-vacuity: nothing is billed, so a warning would be noise the user
        learns to ignore — and then misses the real one."""
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "ollama:qwen3.5:9b"
        status, detail = self._statuses()["price_analysis"]
        self.assertEqual(status, STATUS_SKIP)
        self.assertIn("locally", detail)

    def test_a_user_mapping_repairs_an_unpriced_model(self) -> None:
        """The whole point of keeping the mapping here: when a label moves, one line
        in the user's own config restores the forecast without a release."""
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:private-build"
        self.assertEqual(self._statuses()["price_analysis"][0], STATUS_WARN)

        (self.config / "price_labels.json").write_text(
            '{"mistral:private-build": "mistral small 4"}', encoding="utf-8")

        self.assertEqual(self._statuses()["price_analysis"][0], STATUS_OK)

    def test_it_never_fails_the_whole_doctor_run(self) -> None:
        """A missing price is no reason to refuse to file documents."""
        os.environ["PROCRAFILER_AI_ANALYSIS_PRIMARY"] = "mistral:no-such-model-anywhere"
        self.assertNotIn(STATUS_FAIL, [c.status for c in check_pricing(self.paths)])


if __name__ == "__main__":
    unittest.main()
