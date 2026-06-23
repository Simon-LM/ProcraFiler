from __future__ import annotations

import unittest
from importlib.metadata import version

import procrafiler


class TestVersion(unittest.TestCase):
    def test_version_is_derived_from_package_metadata(self) -> None:
        # Not a hardcoded constant: setuptools-scm derives it from the git tag, so
        # `__version__` always equals the installed package metadata version.
        self.assertEqual(procrafiler.__version__, version("procrafiler"))
        self.assertTrue(procrafiler.__version__)
        self.assertNotEqual(procrafiler.__version__, "0.2.0")  # the old frozen value is gone


if __name__ == "__main__":
    unittest.main()
