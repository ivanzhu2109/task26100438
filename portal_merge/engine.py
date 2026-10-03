"""合并引擎：提交离线编辑、读取状态、人工确认。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from .store import Store
from .vectors import Vector, compare, dominates, merge


@dataclass(frozen=True)
class Candidate:
    """一个未被因果覆盖的候选版本。"""

    edit_id: str
    value: Optional[str]  # None 表示删除
    vector: Vector
    arrival_seq: int  # 服务端首次受理顺序


@dataclass(frozen=True)
class ResourceState:
    """读取结果：当前显示值与全部候选。"""

    resource_id: str
    display: Optional[str]  # None 表示已删除或不存在
    deleted: bool
    candidates: Tuple[Candidate, ...]


class ConfirmationRejected(Exception):
    """候选集合已变化，确认被拒绝且状态不变。"""


class MergeEngine:
    """门户元数据离线合并库的主入口。

    调用可串行；状态全部持久化在 SQLite 文件中，
    正常关闭再打开后候选、胜者及重投语义不变。
    """

    def __init__(self, db_path: str) -> None:
        self._store = Store(db_path)

    def close(self) -> None:
        self._store.close()

    def __enter__(self) -> "MergeEngine":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---- 提交 ----

    def submit_edit(
        self,
        resource_id: str,
        edit_id: str,
        device_id: str,
        vector: Vector,
        value: Optional[str],
    ) -> bool:
        """提交一条离线编辑。value 为 None 表示删除。

        返回 True 表示首次受理，False 表示重投（幂等忽略，
        不重新排序、不增加候选）。
        """
        with self._store.transaction():
            if self._store.has_edit(resource_id, edit_id):
                return False
            self._apply_new_edit(
                resource_id, edit_id, device_id, dict(vector), value, False
            )
            return True

    def _apply_new_edit(
        self,
        resource_id: str,
        edit_id: str,
        device_id: str,
        vector: Vector,
        value: Optional[str],
        is_resolution: bool,
    ) -> None:
        """在事务内登记一条新编辑并增量维护候选集合。"""
        seq = self._store.insert_edit(
            resource_id, edit_id, device_id, vector, value, is_resolution
        )
        candidates = self._store.list_candidates(resource_id)
        # 因果更旧的编辑即使最后到达也不能成为候选。
        for _, _, cand_vector, _, _ in candidates:
            if dominates(cand_vector, vector):
                return
        # 只覆盖部分候选时，未被覆盖者仍保留。
        for cand_id, _, cand_vector, _, _ in candidates:
            if dominates(vector, cand_vector):
                self._store.delete_candidate(resource_id, cand_id)
        self._store.insert_candidate(
            resource_id, edit_id, device_id, vector, value, seq
        )

    # ---- 读取 ----

    def read_state(self, resource_id: str) -> ResourceState:
        """读取当前显示值与全部未被因果覆盖的候选。

        并发候选中按服务端首次受理顺序后写胜出。
        """
        rows = self._store.list_candidates(resource_id)
        candidates = tuple(
            Candidate(
                edit_id=edit_id,
                value=value,
                vector=vector,
                arrival_seq=arrival_seq,
            )
            for edit_id, _, vector, value, arrival_seq in rows
        )
        if not candidates:
            return ResourceState(
                resource_id=resource_id,
                display=None,
                deleted=False,
                candidates=(),
            )
        winner = max(candidates, key=lambda c: c.arrival_seq)
        return ResourceState(
            resource_id=resource_id,
            display=winner.value,
            deleted=winner.value is None,
            candidates=candidates,
        )

    # ---- 人工确认 ----

    def confirm(
        self,
        resource_id: str,
        seen_edit_ids: List[str],
        chosen_edit_id: str,
        resolution_edit_id: str,
        device_id: str,
    ) -> None:
        """人工确认：指明所见完整候选集合并选择其一（含删除候选）。

        若实际候选集合与 seen_edit_ids 不一致，抛出
        ConfirmationRejected 且状态不变；否则生成因果覆盖
        全部所见候选的决议版本，后续编辑可引用它。
        """
        with self._store.transaction():
            if self._store.has_edit(resource_id, resolution_edit_id):
                return  # 决议重投，幂等忽略
            rows = self._store.list_candidates(resource_id)
            actual = {row[0] for row in rows}
            if set(seen_edit_ids) != actual:
                raise ConfirmationRejected(
                    f"候选集合已变化：期望 {sorted(seen_edit_ids)}，"
                    f"实际 {sorted(actual)}"
                )
            if chosen_edit_id not in actual:
                raise ConfirmationRejected(
                    f"所选候选 {chosen_edit_id} 不在当前候选集合中"
                )
            # 决议是独立的因果事件：合并全部所见候选向量后，
            # 递增本设备计数，严格覆盖全部所见候选。
            merged: Vector = {}
            chosen_value: Optional[str] = None
            for edit_id, _, vector, value, _ in rows:
                merged = merge(merged, vector)
                if edit_id == chosen_edit_id:
                    chosen_value = value
            merged[device_id] = merged.get(device_id, 0) + 1
            self._apply_new_edit(
                resource_id,
                resolution_edit_id,
                device_id,
                merged,
                chosen_value,
                True,
            )
