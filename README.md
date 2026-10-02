# 空气污染源许可与合规检查

管理设施、排放口、治理设备、现场检查、整改和许可续期。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8314
```

默认端口为`8314`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`，`GET .../records?kind=inspection` 可按类型过滤
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`
- `GET /api/items/{id}/quota`：授予/已排/剩余额度与额度版本
- `GET /api/items/{id}/ledger`、`GET /api/ledger?item_id=...`：配额台账（只追加）
- `POST /api/items/{id}/quota/adjust`：额度调整，必须提交`expected_version`
- `POST /api/emission-batches`：园区排放批单入账
- `POST /api/emission-batches/{batch_no}/recover`：写入失败后按批单号恢复
- `GET /api/emission-batches?status=posted|failed|received`
- `GET /api/batch-conflicts?batch_no=...`：晚到且内容不同的冲突批单
- `POST /api/ledger/backfill`：旧数据缺少台账时按已批准许可回填

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 可恢复的入账流程

- **批单幂等**：同一`batch_no`只接收第一次结果。内容完全相同的重投返回首次结果（`replay=true`），不重复写台账；晚到但内容不同（按`permit_ref/pollutant/amount/period`的哈希判定）写入`batch_conflicts`并返回409，不覆盖已入账数据。
- **写入恢复**：批单先入收件箱（`received`），入账写台账失败或引用许可尚不存在时标记`failed`；许可补齐后调用`recover`按批单号重放。usage台账以`emission_batch:{batch_no}`为唯一键，恢复绝不重复入账。
- **额度调整并发控制**：许可批准即按批准额度写入`grant`台账并将`quota_version`置1。两人同时基于同一版本调整时先到生效，后到者收到409，响应`details`包含`current_version`、`current_granted`、`your_projected_granted`、`version_gap`；基于当前版本重试才能提交。调整后授予额度不得低于已排放量。
- **台账只追加**：`grant/adjustment/usage`三类分录永不修改；当前余额由台账求和得出。
- **回填旧数据**：状态为`approved`但缺少`grant`台账的许可，可执行backfill按批准额度补齐，操作本身幂等。
- **检查记录留痕**：现场检查/整改记录与许可、批单入账信封链接，历史记录持续可查，信封内含`inspection_records`与`open_rectifications`计数；所有动作进SHA-256审计链。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
