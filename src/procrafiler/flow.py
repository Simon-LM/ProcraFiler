from __future__ import annotations

INITIAL_STATE = "INBOX_NEW"

_TRANSITIONS: dict[str, set[str]] = {
    "INBOX_NEW": {"INBOX_QUEUED", "ERROR_RETRYABLE", "ERROR_BLOCKING"},
    "INBOX_QUEUED": {"PROCESSING_LOCKED", "ERROR_RETRYABLE", "ERROR_BLOCKING"},
    "PROCESSING_LOCKED": {"ANALYSIS_RUNNING", "ERROR_RETRYABLE", "ERROR_BLOCKING"},
    "ANALYSIS_RUNNING": {
        "DUPLICATE_CANDIDATE",
        "CLASSIFICATION_READY",
        "ERROR_RETRYABLE",
        "ERROR_BLOCKING",
    },
    "DUPLICATE_CANDIDATE": {"INBOX_TRASH_PENDING_MANUAL", "ERROR_RETRYABLE", "ERROR_BLOCKING"},
    "CLASSIFICATION_READY": {"ROUTE_PROPOSED", "ERROR_RETRYABLE", "ERROR_BLOCKING"},
    "ROUTE_PROPOSED": {
        "TAXONOMY_UPDATE_REQUIRED",
        "ROUTE_CONFIRMED",
        "USER_CONFIRMATION_REQUIRED",
        "ERROR_RETRYABLE",
        "ERROR_BLOCKING",
    },
    "TAXONOMY_UPDATE_REQUIRED": {"USER_CONFIRMATION_REQUIRED", "ROUTE_CONFIRMED", "ERROR_BLOCKING"},
    "USER_CONFIRMATION_REQUIRED": {"ROUTE_CONFIRMED", "LIBRARY_TRASHED", "ERROR_BLOCKING"},
    "ROUTE_CONFIRMED": {"LIBRARY_STORED", "ERROR_RETRYABLE", "ERROR_BLOCKING"},
    "LIBRARY_STORED": {"LIBRARY_TRASHED", "ERROR_BLOCKING"},
    "INBOX_TRASH_PENDING_MANUAL": {"ERROR_BLOCKING"},
    "LIBRARY_TRASHED": {"ERROR_BLOCKING"},
}

ALL_STATES: frozenset[str] = frozenset(
    set(_TRANSITIONS.keys())
    | {state for targets in _TRANSITIONS.values() for state in targets}
)


class InvalidTransition(ValueError):
    """Raised when the pipeline attempts a transition not declared in _TRANSITIONS."""


def can_transition(current_state: str, next_state: str) -> bool:
    return next_state in _TRANSITIONS.get(current_state, set())


def validate_transition(current_state: str, next_state: str) -> str:
    """Return next_state if the transition is legal, else raise InvalidTransition.

    The pipeline calls this every time it advances the state machine. Raising
    rather than returning False makes accidental illegal jumps loud — they
    should never happen in production, and silent acceptance would let bugs
    rot until the spec drifts from the code (see audit 2026-05).
    """
    if next_state not in ALL_STATES:
        raise InvalidTransition(f"unknown target state: {next_state!r}")
    if not can_transition(current_state, next_state):
        raise InvalidTransition(
            f"illegal transition: {current_state!r} -> {next_state!r}"
        )
    return next_state
