"""Domain-specific exceptions."""

from __future__ import annotations


class HotPathError(Exception):
    """Base exception for the hot-path service."""


class ModelNotLoadedError(HotPathError):
    """Raised when the fastText model is not yet loaded at boot."""


class ProfileNotFoundError(HotPathError):
    """Raised when a user profile does not exist in Cosmos DB.

    DECISION: 2026-05-27 — per SPEC §5.14 a missing profile is NOT an error;
    the pipeline creates an empty default profile. This exception is only used
    internally within the repository layer before that default is applied.
    """

    def __init__(self, user_id: str) -> None:
        super().__init__(f"Profile not found for user: {user_id}")
        self.user_id = user_id


class ClassificationError(HotPathError):
    """Raised when classification fails unrecoverably."""


class DeadLetterError(HotPathError):
    """Raised to signal that a message should be dead-lettered."""
