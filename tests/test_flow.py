import unittest

from procrafiler.flow import can_transition


class TestFlow(unittest.TestCase):
    def test_valid_transition(self) -> None:
        self.assertTrue(can_transition("INBOX_NEW", "INBOX_QUEUED"))

    def test_invalid_transition(self) -> None:
        self.assertFalse(can_transition("INBOX_NEW", "LIBRARY_STORED"))


if __name__ == "__main__":
    unittest.main()
