"""合并库自动化测试：含基于完整历史独立推导的参考实现。"""

from __future__ import annotations

import os
import random
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portal_merge import ConfirmationRejected, MergeEngine
from portal_merge.vectors import compare, dominates, merge


def reference_state(history):
    """参考实现：从完整历史独立推导候选集合与显示结果。

    history: 按受理顺序排列的 (edit_id, vector, value) 列表，
    已按 edit_id 去重（保留首次受理）。
    """
    candidates = []
    for i, (eid, vec, val) in enumerate(history):
        covered = False
        for j, (eid2, vec2, _) in enumerate(history):
            if i != j and dominates(vec2, vec):
                covered = True
                break
        if not covered:
            candidates.append((eid, vec, val, i))
    if not candidates:
        return None, False, []
    winner = max(candidates, key=lambda c: c[3])
    return winner[2], winner[2] is None, [c[0] for c in candidates]


def dedup(history):
    seen = set()
    out = []
    for item in history:
        if item[0] not in seen:
            seen.add(item[0])
            out.append(item)
    return out


class TestVectors(unittest.TestCase):
    def test_compare(self):
        self.assertEqual(compare({"a": 1}, {"a": 1}), 0)
        self.assertEqual(compare({"a": 2}, {"a": 1}), 1)
        self.assertEqual(compare({"a": 1}, {"a": 2}), -1)
        self.assertEqual(compare({"a": 1}, {"b": 1}), 2)
        self.assertEqual(compare({}, {}), 0)
        self.assertEqual(compare({"a": 1, "b": 0}, {"a": 1}), 0)

    def test_merge(self):
        self.assertEqual(
            merge({"a": 1, "b": 2}, {"b": 5, "c": 1}),
            {"a": 1, "b": 5, "c": 1},
        )


class EngineTestBase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "state.db")
        self.eng = MergeEngine(self.path)

    def tearDown(self):
        self.eng.close()

    def reopen(self):
        self.eng.close()
        self.eng = MergeEngine(self.path)


class TestOutOfOrderAndRedelivery(EngineTestBase):
    def test_late_older_edit_not_candidate(self):
        """因果更旧的编辑即使最后到达也不能成为候选。"""
        self.eng.submit_edit("r", "e2", "A", {"A": 2}, "new")
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "old")
        st = self.eng.read_state("r")
        self.assertEqual([c.edit_id for c in st.candidates], ["e2"])
        self.assertEqual(st.display, "new")

    def test_partial_cover_keeps_others(self):
        """新编辑只覆盖部分候选时，未被覆盖者保留。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        self.eng.submit_edit("r", "e3", "A", {"A": 2}, "a2")  # 只覆盖 e1
        st = self.eng.read_state("r")
        self.assertEqual({c.edit_id for c in st.candidates}, {"e2", "e3"})

    def test_redelivery_no_reorder_no_new_candidate(self):
        """重投不得重新排序或增加候选。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        before = self.eng.read_state("r")
        self.assertFalse(self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1"))
        after = self.eng.read_state("r")
        self.assertEqual(before, after)
        self.assertEqual(after.display, "b1")

    def test_random_against_reference(self):
        """乱序+重投下，候选与显示结果与参考实现一致。"""
        rng = random.Random(42)
        for trial in range(30):
            devices = ["A", "B", "C", "D"]
            counters = {d: 0 for d in devices}
            edits = []
            for i in range(rng.randint(2, 20)):
                dev = rng.choice(devices)
                counters[dev] += 1
                vec = dict(counters)
                edits.append((f"t{trial}-e{i}", vec, f"v{i}"))
            # 乱序 + 随机重投
            shuffled = edits[:]
            rng.shuffle(shuffled)
            stream = []
            for e in shuffled:
                stream.append(e)
                if rng.random() < 0.4:
                    stream.append(e)  # 重投
            eng = MergeEngine(os.path.join(self.dir, f"fuzz{trial}.db"))
            for eid, vec, val in stream:
                eng.submit_edit("r", eid, "dev", vec, val)
            st = eng.read_state("r")
            ref_display, ref_deleted, ref_cands = reference_state(dedup(edits))
            self.assertEqual(
                sorted(c.edit_id for c in st.candidates),
                sorted(ref_cands),
                f"trial {trial}",
            )
            self.assertEqual(st.display, ref_display)
            self.assertEqual(st.deleted, ref_deleted)
            eng.close()


class TestConfirm(EngineTestBase):
    def test_confirm_resolution_dominates(self):
        """决议因果覆盖全部所见候选，后续编辑可引用它。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        self.eng.confirm("r", ["e1", "e2"], "e1", "res1", "C")
        st = self.eng.read_state("r")
        self.assertEqual([c.edit_id for c in st.candidates], ["res1"])
        self.assertEqual(st.display, "a1")
        # 后续编辑引用决议：覆盖决议成为唯一候选
        self.eng.submit_edit("r", "e3", "C", {"A": 1, "B": 1, "C": 2}, "c1")
        st = self.eng.read_state("r")
        self.assertEqual([c.edit_id for c in st.candidates], ["e3"])

    def test_unseen_resolution_edit_conflicts_again(self):
        """确认后到达的、未观察到决议的新编辑重新形成冲突。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        self.eng.confirm("r", ["e1", "e2"], "e1", "res1", "C")
        # 另一设备基于同一批旧候选产生的新编辑
        self.eng.submit_edit("r", "e3", "B", {"B": 2}, "b2")
        st = self.eng.read_state("r")
        self.assertEqual({c.edit_id for c in st.candidates}, {"res1", "e3"})

    def test_stale_confirm_rejected_no_side_effect(self):
        """候选变化后的旧页面确认无副作用拒绝。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        before = self.eng.read_state("r")
        with self.assertRaises(ConfirmationRejected):
            self.eng.confirm("r", ["e1"], "e1", "res1", "C")
        self.assertEqual(self.eng.read_state("r"), before)
        # 被拒的决议编号不应残留
        self.eng.confirm("r", ["e1", "e2"], "e2", "res1", "C")
        self.assertEqual(self.eng.read_state("r").display, "b1")

    def test_covered_edit_redelivery_no_reopen(self):
        """已被决议覆盖的旧编辑重投不得重开冲突。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        self.eng.confirm("r", ["e1", "e2"], "e1", "res1", "C")
        self.assertFalse(self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1"))
        st = self.eng.read_state("r")
        self.assertEqual([c.edit_id for c in st.candidates], ["res1"])

    def test_confirm_delete_candidate(self):
        """可以选择删除候选。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"B": 1}, None)
        self.eng.confirm("r", ["e1", "e2"], "e2", "res1", "C")
        st = self.eng.read_state("r")
        self.assertTrue(st.deleted)
        self.assertIsNone(st.display)


class TestDeleteAndPersistence(EngineTestBase):
    def test_delete_wins_but_candidates_queryable(self):
        """删除获胜时显示已删除，但仍能查询其他并发候选。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"B": 1}, None)
        st = self.eng.read_state("r")
        self.assertTrue(st.deleted)
        self.assertEqual({c.edit_id for c in st.candidates}, {"e1", "e2"})

    def test_reopen_then_redeliver_covered_edit(self):
        """删除后重开库再重投被覆盖旧编辑：不复活、不改排序。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"A": 1, "B": 1}, None)
        st = self.eng.read_state("r")
        self.assertTrue(st.deleted)
        self.assertEqual([c.edit_id for c in st.candidates], ["e2"])
        self.reopen()
        self.assertFalse(self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1"))
        st = self.eng.read_state("r")
        self.assertTrue(st.deleted)
        self.assertEqual([c.edit_id for c in st.candidates], ["e2"])

    def test_persistence_of_candidates_and_winner(self):
        """正常关闭再打开后，候选、胜者及重投语义不变。"""
        self.eng.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        self.eng.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        before = self.eng.read_state("r")
        self.reopen()
        after = self.eng.read_state("r")
        self.assertEqual(before, after)
        self.assertFalse(self.eng.submit_edit("r", "e2", "B", {"B": 1}, "b1"))
        self.assertEqual(self.eng.read_state("r"), before)

    def test_first_acceptance_order_not_device_order(self):
        """按服务端首次受理顺序后写胜出，不看设备时间。"""
        # e2 向量上更"新"（计数更高）但先受理；e1 后受理
        self.eng.submit_edit("r", "e2", "A", {"A": 2, "B": 1}, "newer")
        self.eng.submit_edit("r", "e1", "B", {"B": 1}, "older-but-late")
        # e1 被 e2 因果覆盖，不是候选
        st = self.eng.read_state("r")
        self.assertEqual([c.edit_id for c in st.candidates], ["e2"])
        # 真正的并发场景：先受理的输，后受理的赢
        self.eng.submit_edit("s", "x1", "A", {"A": 1}, "first")
        self.eng.submit_edit("s", "x2", "B", {"B": 1}, "second")
        self.assertEqual(self.eng.read_state("s").display, "second")


if __name__ == "__main__":
    unittest.main()
