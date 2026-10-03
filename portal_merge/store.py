"""SQLite 持久化层：编辑记录、候选集合与受理顺序。

表设计：
- edits：全部已受理编辑（含被覆盖者），edit_id 用于重投去重，
  seq 为全局自增的首次受理顺序。
- candidates：当前未被因果覆盖的候选，随提交增量维护，
  正常关闭再打开后语义不变。
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from typing import Iterator, List, Optional, Tuple

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS edits (
    seq INTEGER PRIMARY KEY AUTOINCREMENT,
    resource_id TEXT NOT NULL,
    edit_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    vector TEXT NOT NULL,
    value TEXT,
    is_resolution INTEGER NOT NULL DEFAULT 0,
    UNIQUE (resource_id, edit_id)
);
CREATE INDEX IF NOT EXISTS idx_edits_resource ON edits (resource_id);
CREATE TABLE IF NOT EXISTS candidates (
    resource_id TEXT NOT NULL,
    edit_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    vector TEXT NOT NULL,
    value TEXT,
    arrival_seq INTEGER NOT NULL,
    PRIMARY KEY (resource_id, edit_id)
);
"""


class Store:
    """管理本地 SQLite 文件中的同步状态。"""

    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT OR IGNORE INTO meta (key, value) VALUES ('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """事务：确认等复合操作要么全部生效要么全部回滚。"""
        try:
            self._conn.execute("BEGIN IMMEDIATE")
            yield
        except Exception:
            self._conn.rollback()
            raise
        else:
            self._conn.commit()

    # ---- edits ----

    def has_edit(self, resource_id: str, edit_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM edits WHERE resource_id = ? AND edit_id = ?",
            (resource_id, edit_id),
        ).fetchone()
        return row is not None

    def insert_edit(
        self,
        resource_id: str,
        edit_id: str,
        device_id: str,
        vector: dict,
        value: Optional[str],
        is_resolution: bool = False,
    ) -> int:
        """插入编辑记录，返回全局受理顺序号。"""
        cur = self._conn.execute(
            "INSERT INTO edits (resource_id, edit_id, device_id, vector, value,"
            " is_resolution) VALUES (?, ?, ?, ?, ?, ?)",
            (
                resource_id,
                edit_id,
                device_id,
                json.dumps(vector, sort_keys=True),
                value,
                1 if is_resolution else 0,
            ),
        )
        return int(cur.lastrowid)

    # ---- candidates ----

    def list_candidates(self, resource_id: str) -> List[Tuple]:
        """返回 (edit_id, device_id, vector, value, arrival_seq) 列表。"""
        rows = self._conn.execute(
            "SELECT edit_id, device_id, vector, value, arrival_seq"
            " FROM candidates WHERE resource_id = ? ORDER BY arrival_seq",
            (resource_id,),
        ).fetchall()
        return [
            (r[0], r[1], json.loads(r[2]), r[3], r[4]) for r in rows
        ]

    def insert_candidate(
        self,
        resource_id: str,
        edit_id: str,
        device_id: str,
        vector: dict,
        value: Optional[str],
        arrival_seq: int,
    ) -> None:
        self._conn.execute(
            "INSERT INTO candidates"
            " (resource_id, edit_id, device_id, vector, value, arrival_seq)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                resource_id,
                edit_id,
                device_id,
                json.dumps(vector, sort_keys=True),
                value,
                arrival_seq,
            ),
        )

    def delete_candidate(self, resource_id: str, edit_id: str) -> None:
        self._conn.execute(
            "DELETE FROM candidates WHERE resource_id = ? AND edit_id = ?",
            (resource_id, edit_id),
        )
