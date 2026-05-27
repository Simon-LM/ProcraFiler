import unittest

from procrafiler.flow import InvalidTransition, can_transition, validate_transition


class TestFlow(unittest.TestCase):
    def test_valid_transition(self) -> None:
        self.assertTrue(can_transition("INBOX_NEW", "INBOX_QUEUED"))

    def test_invalid_transition(self) -> None:
        self.assertFalse(can_transition("INBOX_NEW", "LIBRARY_STORED"))

    def test_validate_transition_returns_next_state_on_legal_jump(self) -> None:
        self.assertEqual(validate_transition("INBOX_NEW", "INBOX_QUEUED"), "INBOX_QUEUED")

    def test_validate_transition_raises_on_illegal_jump(self) -> None:
        with self.assertRaises(InvalidTransition) as ctx:
            validate_transition("INBOX_NEW", "LIBRARY_STORED")
        self.assertIn("INBOX_NEW", str(ctx.exception))
        self.assertIn("LIBRARY_STORED", str(ctx.exception))

    def test_validate_transition_raises_on_unknown_target(self) -> None:
        with self.assertRaises(InvalidTransition):
            validate_transition("INBOX_NEW", "DOES_NOT_EXIST")


if __name__ == "__main__":
    unittest.main()
