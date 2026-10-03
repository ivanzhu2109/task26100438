"""Public API: submit offline edits, read merged state, confirm resolutions."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from .store import Store, StoredEdit
from .vectors import covers, join


@dataclass(frozen=True)
class Candidate:
    edit_id: str
    device_id: str
    vector: dict
    value: str | None
    deleted: bool
    seq: int


@dataclass(frozen=True)
class ResourceState:
    resource_id: str
    deleted: bool
    value: str | None  # None when deleted or when the resource has no edits
    winner_edit_id: str | None
    candidates: tuple[Candidate, ...] = ()


@dataclass(frozen=True)
class SubmitResult:
    edit_id: str
    duplicate: bool  # True when this edit_id was already accepted before
    is_candidate: bool
    seq: int


@dataclass(frozen=True)
class ConfirmResult:
    accepted: bool
    resolution_edit_id: str | None = None
    reason: str | None = None


class MergeStore:
    """Offline merge library backed by a local SQLite file."""

    def __init__(self, db_path: str) -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "MergeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @staticmethod
    def _to_candidate(edit: StoredEdit) -> Candidate:
        return Candidate(
            edit_id=edit.edit_id,
            device_id=edit.device_id,
            vector=edit.vector,
            value=edit.value,
            deleted=edit.deleted,
            seq=edit.seq,
        )

    def submit_edit(
        self,
        resource_id: str,
        edit_id: str,
        device_id: str,
        vector: dict,
        value: str | None = None,
        deleted: bool = False,
    ) -> SubmitResult:
        """Accept (or dedupe) one offline edit."""
        existing = self._store.get_edit(edit_id)
        if existing is not None:
            is_candidate = any(
                c.edit_id == edit_id
                for c in self._store.get_candidates(existing.resource_id)
            )
            return SubmitResult(
                edit_id=edit_id, duplicate=True, is_candidate=is_candidate,
                seq=existing.seq,
            )

        candidates = self._store.get_candidates(resource_id)
        if any(covers(c.vector, vector) for c in candidates):
            # Causally older than an already-accepted version: record for
            # dedupe/ordering, but it can never become a candidate.
            with self._store.transaction():
                stored = self._store.insert_edit(
                    resource_id, edit_id, device_id, vector, value, deleted
                )
            return SubmitResult(
                edit_id=edit_id, duplicate=False, is_candidate=False,
                seq=stored.seq,
            )

        survivors = [c for c in candidates if not covers(vector, c.vector)]
        with self._store.transaction():
            stored = self._store.insert_edit(
                resource_id, edit_id, device_id, vector, value, deleted
            )
            self._store.replace_candidates(
                resource_id, [c.edit_id for c in survivors] + [edit_id]
            )
        return SubmitResult(
            edit_id=edit_id, duplicate=False, is_candidate=True, seq=stored.seq
        )

    def read_state(self, resource_id: str) -> ResourceState:
        """Return display value plus all non-covered candidates."""
        candidates = self._store.get_candidates(resource_id)
        if not candidates:
            return ResourceState(
                resource_id=resource_id, deleted=False, value=None,
                winner_edit_id=None,
            )
        winner = max(candidates, key=lambda c: c.seq)
        return ResourceState(
            resource_id=resource_id,
            deleted=winner.deleted,
            value=None if winner.deleted else winner.value,
            winner_edit_id=winner.edit_id,
            candidates=tuple(self._to_candidate(c) for c in candidates),
        )

    def confirm(
        self,
        resource_id: str,
        seen_edit_ids: list[str],
        chosen_edit_id: str,
        device_id: str,
        edit_id: str | None = None,
    ) -> ConfirmResult:
        """Resolve a conflict by adopting one of the seen candidates."""
        candidates = self._store.get_candidates(resource_id)
        actual_ids = {c.edit_id for c in candidates}
        if set(seen_edit_ids) != actual_ids:
            return ConfirmResult(
                accepted=False, reason="candidate set changed"
            )
        chosen = next((c for c in candidates if c.edit_id == chosen_edit_id), None)
        if chosen is None:
            return ConfirmResult(
                accepted=False, reason="chosen edit is not a candidate"
            )

        base: dict = {}
        for c in candidates:
            base = join(base, c.vector)
        base[device_id] = base.get(device_id, 0) + 1

        resolution_id = edit_id or f"resolve-{uuid.uuid4().hex}"
        with self._store.transaction():
            self._store.insert_edit(
                resource_id, resolution_id, device_id, base,
                chosen.value, chosen.deleted,
            )
            self._store.replace_candidates(resource_id, [resolution_id])
        return ConfirmResult(accepted=True, resolution_edit_id=resolution_id)
