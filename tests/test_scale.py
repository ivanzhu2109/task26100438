"""Reproducible scale case: 200k edits, one resource with 100k edits,
up to 16 devices per resource. P95 of single submit and single-resource
read must stay under 100ms.

Run only this file:  python3 -m pytest tests/test_scale.py -q
"""

import random
import statistics
import time

import pytest

from portal_merge import MergeStore
from portal_merge.vectors import covers

SEED = 20261003
HOT_EDITS = 100_000
OTHER_RESOURCES = 2_000
OTHER_EDITS_PER_RESOURCE = 50  # 2000 * 50 = 100k -> 200k total
DEVICES_PER_RESOURCE = 16
P95_LIMIT_MS = 100.0


def gen_resource_edits(rng, num_edits, num_devices):
    """Causally consistent edit stream for one resource (mostly chained,
    with occasional concurrent branches and deletes)."""
    devices = [f"d{i}" for i in range(num_devices)]
    knowledge = {d: {} for d in devices}
    edits = []
    for i in range(num_edits):
        dev = rng.choice(devices)
        if rng.random() < 0.7:  # usually sync up before editing
            other = rng.choice(devices)
            for k, v in knowledge[other].items():
                knowledge[dev][k] = max(knowledge[dev].get(k, 0), v)
        vec = dict(knowledge[dev])
        vec[dev] = vec.get(dev, 0) + 1
        knowledge[dev] = dict(vec)
        deleted = rng.random() < 0.02
        edits.append((dev, vec, None if deleted else f"v{i}", deleted))
    return edits


def p95(samples):
    ordered = sorted(samples)
    return ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))]


@pytest.mark.parametrize("db_name", ["scale.db"])
def test_scale(tmp_path, db_name):
    rng = random.Random(SEED)
    ms = MergeStore(str(tmp_path / db_name))

    submit_ms = []
    read_ms = []
    total = 0

    def submit_stream(resource_id, edits):
        nonlocal total
        mirror = []  # independent candidate mirror for invariant checks
        for i, (dev, vec, value, deleted) in enumerate(edits):
            edit_id = f"{resource_id}-e{i}"
            start = time.perf_counter()
            res = ms.submit_edit(resource_id, edit_id, dev, vec, value, deleted)
            submit_ms.append((time.perf_counter() - start) * 1000)
            total += 1
            if rng.random() < 0.05:  # redelivery must be a cheap no-op
                again = ms.submit_edit(resource_id, edit_id, dev, vec, value, deleted)
                assert again.duplicate and again.seq == res.seq
                total += 1
            # mirror the candidate rule independently
            if not any(covers(c, vec) for c in mirror):
                mirror = [c for c in mirror if not covers(vec, c)]
                mirror.append(vec)
            if i % 1000 == 999:
                state = ms.read_state(resource_id)
                assert len(state.candidates) == len(mirror)
                assert len(state.candidates) <= DEVICES_PER_RESOURCE

    # Hot resource: 100k edits, 16 devices.
    hot = gen_resource_edits(rng, HOT_EDITS, DEVICES_PER_RESOURCE)
    submit_stream("hot", hot)

    # 2000 more resources x 50 edits -> 200k edits in total.
    for r in range(OTHER_RESOURCES):
        edits = gen_resource_edits(rng, OTHER_EDITS_PER_RESOURCE, DEVICES_PER_RESOURCE)
        submit_stream(f"res{r}", edits)

    assert total >= 200_000

    # Single-resource reads on the hot resource (after reopen, cold cache).
    ms.close()
    ms = MergeStore(str(tmp_path / db_name))
    for _ in range(200):
        start = time.perf_counter()
        state = ms.read_state("hot")
        read_ms.append((time.perf_counter() - start) * 1000)
    assert state.candidates
    ms.close()

    submit_p95 = p95(submit_ms)
    read_p95 = p95(read_ms)
    print(f"\nsubmits={len(submit_ms)} submit_p95={submit_p95:.2f}ms "
          f"read_p95={read_p95:.2f}ms submit_mean={statistics.mean(submit_ms):.2f}ms")
    assert submit_p95 <= P95_LIMIT_MS
    assert read_p95 <= P95_LIMIT_MS
