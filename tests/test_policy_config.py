# pyright: reportUnknownVariableType=false
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from procrafiler.config import (
    default_runtime_paths,
    ensure_runtime_layout,
    load_runtime_policy,
)


class TestPolicyConfig(unittest.TestCase):
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

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_policy_created(self) -> None:
        policy = load_runtime_policy(self.paths)
        self.assertEqual(policy.mirror_retention_days, 30)
        self.assertEqual(policy.mirror_versions_keep, 3)
        self.assertEqual(policy.taxonomy_max_depth, 6)
        self.assertTrue(self.paths.policy_file.exists())

    def test_policy_override_from_toml(self) -> None:
        self.paths.policy_file.write_text(
            "[mirror]\nretention_days = 45\nversions_keep = 5\n\n[taxonomy]\nmax_depth = 8\n",
            encoding="utf-8",
        )
        policy = load_runtime_policy(self.paths)
        self.assertEqual(policy.mirror_retention_days, 45)
        self.assertEqual(policy.mirror_versions_keep, 5)
        self.assertEqual(policy.taxonomy_max_depth, 8)


if __name__ == "__main__":
    unittest.main()
