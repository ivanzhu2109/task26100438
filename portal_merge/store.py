"""SQLite persistence layer for the offline merge library.

Schema:
- edits: every accepted edit (dedupe by edit_id, server acceptance order seq).
- candidates: current maximal (non-covered) versions per resource.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass


@dataclass(frozen=True)
class StoredEdit:
    resource_id: str
    edit_id: str
    device_id: str
    vector: dict  # dict[str, int]
    value: str | None
    deleted: bool
    seq: int  # server first-acceptance order, 1-based, gapless per store


class Store:
    """Thin SQLite wrapper. All state lives in one database file."""

    def __init__(self, db_path: str) -> None:
        self._conn = sqlite3.connect(db_path, isolation_level=None)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS edits (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                edit_id TEXT NOT NULL UNIQUE,
                resource_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                vector TEXT NOT NULL,
                value TEXT,
                deleted INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS idx_edits_resource ON edits(resource_id);
            CREATE TABLE IF NOT EXISTS candidates (
                resource_id TEXT NOT NULL,
                edit_id TEXT NOT NULL,
                PRIMARY KEY (resource_id, edit_id)
            );
            """
        )

    def close(self) -> None:
        self._conn.close()

    @contextmanager
    def transaction(self):
        """Group several writes into one atomic unit (one commit)."""
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            yield
        except BaseException:
            self._conn.execute("ROLLBACK")
            raise
        else:
            self._conn.execute("COMMIT")

    @staticmethod
    def _row_to_edit(row: tuple) -> StoredEdit:
        return StoredEdit(
            resource_id=row[0],
            edit_id=row[1],
            device_id=row[2],
            vector=json.loads(row[3]),
            value=row[4],
            deleted=bool(row[5]),
            seq=row[6],
        )

    _COLUMNS = "resource_id, edit_id, device_id, vector, value, deleted, seq"

    def get_edit(self, edit_id: str) -> StoredEdit | None:
        """Fetch an accepted edit by its globally unique id (dedupe)."""
        row = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM edits WHERE edit_id = ?", (edit_id,)
        ).fetchone()
        return self._row_to_edit(row) if row else None

    def insert_edit(
        self,
        resource_id: str,
        edit_id: str,
        device_id: str,
        vector: dict,
        value: str | None,
        deleted: bool,
    ) -> StoredEdit:
        """Insert a new accepted edit, assigning the next acceptance seq."""
        cur = self._conn.execute(
            "INSERT INTO edits (edit_id, resource_id, device_id, vector, value, deleted)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (edit_id, resource_id, device_id, json.dumps(vector, sort_keys=True),
             value, int(deleted)),
        )
        return StoredEdit(
            resource_id=resource_id,
            edit_id=edit_id,
            device_id=device_id,
            vector=dict(vector),
            value=value,
            deleted=deleted,
            seq=cur.lastrowid,
        )

    def get_candidates(self, resource_id: str) -> list[StoredEdit]:
        """Return current candidates for a resource, ordered by seq."""
        rows = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM edits"
            " WHERE edit_id IN (SELECT edit_id FROM candidates WHERE resource_id = ?)"
            " ORDER BY seq",
            (resource_id,),
        ).fetchall()
        return [self._row_to_edit(row) for row in rows]

    def replace_candidates(self, resource_id: str, keep_edit_ids: list[str]) -> None:
        """Replace the candidate set of a resource with the given edit ids."""
        self._conn.execute(
            "DELETE FROM candidates WHERE resource_id = ?", (resource_id,)
        )
        self._conn.executemany(
            "INSERT INTO candidates (resource_id, edit_id) VALUES (?, ?)",
            [(resource_id, eid) for eid in keep_edit_ids],
        )
