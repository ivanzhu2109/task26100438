# task26100438
## portal_merge：门户元数据离线合并库

Python + SQLite 实现的离线合并库，由上层直接调用，同步状态保存在本地 SQLite 文件中。

### 数据模型

- 资源以稳定 ID 区分，整份文本是一个值；删除是独立标记（`deleted=True`），与文本编辑按相同规则参与选择。
- 每条编辑携带全局唯一编号 `edit_id`、设备 ID 与版本向量（`{设备ID: 计数}`，缺失维度视为 0）。
- 向量 A 各维均不小于 B 且至少一维更大时，A 因果覆盖 B；不同且互不覆盖的版本为并发。

### API（`portal_merge.MergeStore`）

- `submit_edit(resource_id, edit_id, device_id, vector, value=None, deleted=False)`
  提交离线编辑。重投（相同 `edit_id`）幂等：不重新排序、不增加候选。
  被已接收版本因果覆盖的编辑只记录用于去重，不会成为候选。
- `read_state(resource_id)` 返回当前显示值（并发候选中按服务端首次受理顺序后写胜出）
  及全部未被覆盖的候选；删除获胜时 `deleted=True`、`value=None`。
- `confirm(resource_id, seen_edit_ids, chosen_edit_id, device_id, edit_id=None)`
  人工确认。`seen_edit_ids` 必须与当前实际候选集合完全一致，否则整次拒绝且状态不变。
  接受后生成采用所选值（含删除）的决议版本，其向量为全部候选向量的 join 再递增
  确认设备自身计数——是独立的因果事件：基于旧候选、未观察到决议的新编辑仍与决议并发，
  已被决议覆盖的旧编辑重投不会重开冲突。

### 持久化

所有状态（全部编辑、首次受理顺序、候选集合）保存在单个 SQLite 文件中
（WAL 模式），正常关闭再打开后候选、胜者及重投语义不变。

### 测试

```bash
python3 -m pytest tests/test_merge.py -q   # 语义验收：乱序/重投/单分支推进/确认/删除重开
python3 -m pytest tests/test_scale.py -q   # 规模用例：20 万条编辑，P95 < 100ms
```

规模用例使用固定随机种子（`SEED = 20261003`），可复现：一个资源 10 万条历史编辑、
每资源最多 16 台设备，并对热资源做周期性候选集合不变量校验。
