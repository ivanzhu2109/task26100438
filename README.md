# 门户元数据离线合并库

Python + SQLite 实现的离线合并库，由上层直接调用，同步状态保存在本地 SQLite 文件中。

## 功能

- `MergeEngine.submit_edit`：提交离线编辑（文本值或删除标记），携带全局唯一编号与设备版本向量；重投幂等。
- `MergeEngine.read_state`：读取当前显示值与全部未被因果覆盖的候选；并发候选按服务端首次受理顺序后写胜出。
- `MergeEngine.confirm`：人工确认，指明所见完整候选集合并选择其一（含删除候选）；候选集合已变化则整次拒绝且状态不变，否则生成因果覆盖全部所见候选的独立决议版本。

## 用法

```python
from portal_merge import MergeEngine, ConfirmationRejected

eng = MergeEngine("sync-state.db")
eng.submit_edit("res1", "edit-1", "devA", {"devA": 1}, "hello")
state = eng.read_state("res1")
try:
    eng.confirm("res1", [c.edit_id for c in state.candidates],
                state.candidates[0].edit_id, "resolution-1", "devB")
except ConfirmationRejected:
    pass  # 候选集合已变化，状态不变
eng.close()
```

## 结构

- `portal_merge/vectors.py`：版本向量比较（因果覆盖 / 并发）。
- `portal_merge/store.py`：SQLite 持久化（编辑记录、候选集合、受理顺序）。
- `portal_merge/engine.py`：合并引擎主入口。
- `tests/test_merge.py`：自动化测试，含基于完整历史独立推导的参考实现。
- `bench/scale_bench.py`：规模用例（20 万条编辑，单资源 10 万条历史，16 台设备）。

## 运行

```sh
python3 -m unittest discover -s tests   # 自动化测试
python3 bench/scale_bench.py            # 规模用例（P95 预算 100ms）
```
