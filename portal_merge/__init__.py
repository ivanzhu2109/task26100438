"""Offline merge library for portal metadata (text resources + deletes)."""

from .core import (
    Candidate,
    ConfirmResult,
    MergeStore,
    ResourceState,
    SubmitResult,
)

__all__ = [
    "Candidate",
    "ConfirmResult",
    "MergeStore",
    "ResourceState",
    "SubmitResult",
]
