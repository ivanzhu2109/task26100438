"""Acceptance tests for the offline merge library."""

import os
import random
import tempfile

import pytest

from portal_merge import MergeStore
from portal_merge.vectors import covers


@pytest.fixture()
def db_path(tmp_path):
    return str(tmp_path / "merge.db")


def reference_state(edits):
    """Independent derivation from full history.

    edits: list of (edit_id, vector, value, deleted, seq) with seq being the
    first-acceptance order. Candidates are versions not causally covered by
    any other received version; the winner is the max-seq candidate.
    """
    candidates = [
        e for e in edits
        if not any(f[0] != e[0] and covers(f[1], e[1]) for f in edits)
    ]
    winner = max(candidates, key=lambda e: e[4]) if candidates else None
    return candidates, winner


def make_history(rng, num_devices, num_edits, delete_prob=0.0):
    """Generate a causally consistent edit history for one resource."""
    devices = [f"dev{i}" for i in range(num_devices)]
    knowledge = {d: {} for d in devices}
    edits = []  # (edit_id, device, vector, value, deleted)
    for i in range(num_edits):
        dev = rng.choice(devices)
        if rng.random() < 0.4:  # sync: merge another device's knowledge
            other = rng.choice(devices)
            for k, v in knowledge[other].items():
                knowledge[dev][k] = max(knowledge[dev].get(k, 0), v)
        vec = dict(knowledge[dev])
        vec[dev] = vec.get(dev, 0) + 1
        knowledge[dev] = dict(vec)
        deleted = rng.random() < delete_prob
        value = None if deleted else f"value-{i}"
        edits.append((f"e{i}", dev, vec, value, deleted))
    return edits


def deliver(ms, resource_id, edits, rng, dup_prob=0.3):
    """Submit edits in random order with random redeliveries.

    Returns first-acceptance seq per edit_id.
    """
    order = list(edits)
    rng.shuffle(order)
    first_seq = {}
    for edit_id, dev, vec, value, deleted in order:
        res = ms.submit_edit(resource_id, edit_id, dev, vec, value, deleted)
        first_seq[edit_id] = res.seq
        if rng.random() < dup_prob:  # redeliver identical payload
            again = ms.submit_edit(resource_id, edit_id, dev, vec, value, deleted)
            assert again.duplicate
            assert again.seq == res.seq
    return first_seq


def test_randomized_matches_reference(db_path):
    rng = random.Random(20261003)
    edits = make_history(rng, num_devices=5, num_edits=400)
    with MergeStore(db_path) as ms:
        first_seq = deliver(ms, "res", edits, rng)
        state = ms.read_state("res")
    history = [
        (eid, vec, value, deleted, first_seq[eid])
        for eid, _dev, vec, value, deleted in edits
    ]
    candidates, winner = reference_state(history)
    assert sorted(c.edit_id for c in state.candidates) == sorted(
        e[0] for e in candidates
    )
    assert state.winner_edit_id == winner[0]
    assert state.deleted == winner[3]
    assert state.value == (None if winner[3] else winner[2])


def test_out_of_order_and_single_branch_advance(db_path):
    with MergeStore(db_path) as ms:
        # Branch point: two concurrent edits.
        ms.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        ms.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        # B advances only its own branch: covers e2, concurrent with e1.
        ms.submit_edit("r", "e3", "B", {"A": 0, "B": 2}, "b2")
        state = ms.read_state("r")
        assert sorted(c.edit_id for c in state.candidates) == ["e1", "e3"]
        assert state.value == "b2"  # e3 accepted last among candidates
        # A advances past e1, then a causally older edit arrives last:
        # it is covered by e4 and must not become a candidate.
        ms.submit_edit("r", "e4", "A", {"A": 2}, "a2")
        late = ms.submit_edit("r", "e0", "A", {"A": 1}, "a1-again")
        assert not late.is_candidate
        state = ms.read_state("r")
        assert sorted(c.edit_id for c in state.candidates) == ["e3", "e4"]


def test_redelivery_no_reorder_no_extra_candidates(db_path):
    with MergeStore(db_path) as ms:
        ms.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        ms.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        before = ms.read_state("r")
        # Redeliver e1: must keep original seq and not duplicate candidates.
        dup = ms.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        assert dup.duplicate and dup.seq == 1
        after = ms.read_state("r")
        assert after == before
        assert after.winner_edit_id == "e2"


def test_confirm_then_unseen_edit_reopens_conflict(db_path):
    with MergeStore(db_path) as ms:
        ms.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        ms.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        res = ms.confirm("r", ["e1", "e2"], "e1", device_id="A", edit_id="res1")
        assert res.accepted
        assert ms.read_state("r").value == "a1"
        # Device C based its edit on the same old candidates without
        # observing the resolution -> concurrent with the resolution.
        ms.submit_edit("r", "e3", "C", {"A": 1, "B": 1, "C": 1}, "c1")
        state = ms.read_state("r")
        assert sorted(c.edit_id for c in state.candidates) == ["e3", "res1"]
        # Redelivery of a resolved-away old edit must not reopen conflict.
        dup = ms.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        assert dup.duplicate
        state = ms.read_state("r")
        assert sorted(c.edit_id for c in state.candidates) == ["e3", "res1"]


def test_stale_confirm_rejected_without_side_effects(db_path):
    with MergeStore(db_path) as ms:
        ms.submit_edit("r", "e1", "A", {"A": 1}, "a1")
        ms.submit_edit("r", "e2", "B", {"B": 1}, "b1")
        # A third concurrent edit changes the candidate set before the
        # user's confirm (issued against the old page) arrives.
        ms.submit_edit("r", "e3", "C", {"C": 1}, "c1")
        before = ms.read_state("r")
        res = ms.confirm("r", ["e1", "e2"], "e1", device_id="A")
        assert not res.accepted
        assert ms.read_state("r") == before
        # Confirming with the up-to-date set works, delete candidate too.
        ms.submit_edit("r", "e4", "D", {"D": 1}, None, deleted=True)
        ids = [c.edit_id for c in ms.read_state("r").candidates]
        res = ms.confirm("r", ids, "e4", device_id="A", edit_id="res1")
        assert res.accepted
        state = ms.read_state("r")
        assert state.deleted and state.value is None


def test_delete_reopen_redeliver_no_resurrection(db_path):
    ms = MergeStore(db_path)
    ms.submit_edit("r", "e1", "A", {"A": 1}, "a1")
    ms.submit_edit("r", "e2", "B", {"A": 1, "B": 1}, None, deleted=True)
    assert ms.read_state("r").deleted
    ms.close()
    # Reopen: state survives; redelivering the covered old edit must not
    # resurrect the resource nor change first-acceptance order.
    ms = MergeStore(db_path)
    state = ms.read_state("r")
    assert state.deleted and state.value is None
    dup = ms.submit_edit("r", "e1", "A", {"A": 1}, "a1")
    assert dup.duplicate and dup.seq == 1
    state = ms.read_state("r")
    assert state.deleted and state.winner_edit_id == "e2"
    assert [c.edit_id for c in state.candidates] == ["e2"]
    # A brand-new edit built on pre-delete history is concurrent, not a
    # resurrection of the covered version.
    ms.submit_edit("r", "e3", "C", {"A": 1, "C": 1}, "c1")
    state = ms.read_state("r")
    assert sorted(c.edit_id for c in state.candidates) == ["e2", "e3"]
    ms.close()


def test_equal_vectors_both_candidates(db_path):
    # Two devices can legitimately produce identical vectors; neither
    # covers the other, so both stay candidates and last-write wins.
    with MergeStore(db_path) as ms:
        ms.submit_edit("r", "e1", "A", {"A": 1, "B": 1}, "a1")
        ms.submit_edit("r", "e2", "B", {"A": 1, "B": 1}, "b1")
        state = ms.read_state("r")
        assert sorted(c.edit_id for c in state.candidates) == ["e1", "e2"]
        assert state.value == "b1"
