from datetime import datetime, timezone
import unittest

from procrafiler.naming import build_timestamped_filename


class TestNaming(unittest.TestCase):
    def test_utc_prefixed_filename(self) -> None:
        ts = datetime(2026, 4, 1, 22, 10, 6, tzinfo=timezone.utc)
        name = build_timestamped_filename("Tax Report 2024.pdf", now_utc=ts)
        self.assertEqual(name, "2026-04-01_22-10-06__Tax-Report-2024.pdf")


if __name__ == "__main__":
    unittest.main()
