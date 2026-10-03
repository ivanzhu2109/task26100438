"""规模用例：20 万条编辑，单资源 10 万条历史，每资源最多 16 台设备。

验证单条提交与单资源读取的 P95 均不超过 100ms。
用法：python3 bench/scale_bench.py [db_path]
"""

from __future__ import annotations

import os
import random
import statistics
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from portal_merge import MergeEngine

TOTAL_EDITS = 200_000
HOT_RESOURCE_EDITS = 100_000
DEVICES_PER_RESOURCE = 16
P95_BUDGET_MS = 100.0


def percentile(samples, pct):
    ordered = sorted(samples)
    rank = max(1, round(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def main() -> int:
    rng = random.Random(20261004)
    db_path = (
        sys.argv[1]
        if len(sys.argv) > 1
        else os.path.join(tempfile.mkdtemp(prefix="merge-bench-"), "bench.db")
    )
    eng = MergeEngine(db_path)

    counters = {}  # (resource, device) -> count
    frontier = {}  # resource -> 合并后的向量（模拟设备所见历史）

    def next_edit(resource, devices):
        dev = rng.choice(devices)
        base = dict(frontier.get(resource, {}))
        key = (resource, dev)
        counters[key] = counters.get(key, 0) + 1
        base[dev] = counters[key]
        frontier[resource] = base
        return dev, base

    submit_ms = []
    read_ms = []

    def timed_submit(resource, edit_id, dev, vec, value):
        start = time.perf_counter()
        eng.submit_edit(resource, edit_id, dev, vec, value)
        submit_ms.append((time.perf_counter() - start) * 1000.0)

    # 1) 热点资源：10 万条历史编辑，16 台设备
    hot_devices = [f"hot-dev{i}" for i in range(DEVICES_PER_RESOURCE)]
    for i in range(HOT_RESOURCE_EDITS):
        dev, vec = next_edit("hot", hot_devices)
        value = None if rng.random() < 0.02 else f"hot-value-{i}"
        timed_submit("hot", f"hot-e{i}", dev, vec, value)
        if i % 10_000 == 0:
            start = time.perf_counter()
            eng.read_state("hot")
            read_ms.append((time.perf_counter() - start) * 1000.0)

    # 2) 其余 10 万条分散到 1 万个资源，制造并发与重投
    for i in range(TOTAL_EDITS - HOT_RESOURCE_EDITS):
        resource = f"r{i % 10_000}"
        devices = [f"{resource}-d{j}" for j in range(DEVICES_PER_RESOURCE)]
        dev, vec = next_edit(resource, devices)
        timed_submit(resource, f"cold-e{i}", dev, vec, f"value-{i}")
        if rng.random() < 0.05:  # 重投已受理编辑
            eng.submit_edit(resource, f"cold-e{i}", dev, vec, f"value-{i}")

    # 3) 关闭重开后测量单资源读取
    eng.close()
    eng = MergeEngine(db_path)
    for _ in range(200):
        start = time.perf_counter()
        eng.read_state("hot")
        read_ms.append((time.perf_counter() - start) * 1000.0)
    for i in range(200):
        resource = f"r{i}"
        start = time.perf_counter()
        eng.read_state(resource)
        read_ms.append((time.perf_counter() - start) * 1000.0)
    eng.close()

    p95_submit = percentile(submit_ms, 95)
    p95_read = percentile(read_ms, 95)
    size_mb = os.path.getsize(db_path) / 1e6
    print("db:", db_path, "(%.1f MB)" % size_mb)
    print("submit samples:", len(submit_ms), " read samples:", len(read_ms))
    print("submit P95: %.3f ms  mean: %.3f ms" % (p95_submit, statistics.mean(submit_ms)))
    print("read P95: %.3f ms  mean: %.3f ms" % (p95_read, statistics.mean(read_ms)))
    ok = p95_submit <= P95_BUDGET_MS and p95_read <= P95_BUDGET_MS
    print("P95 budget %.0f ms: %s" % (P95_BUDGET_MS, "PASS" if ok else "FAIL"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
