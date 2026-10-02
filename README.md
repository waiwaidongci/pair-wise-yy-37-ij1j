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
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

### 排放批单入账（按批单号幂等）

- `POST /api/batches`：接收排放批单。同一`batch_no`只接收第一次结果（201）；相同内容重放为幂等返回（200）；不同内容晚到返回409并留作冲突。
- `GET /api/batches`：批单列表。
- `GET /api/batches/{batch_no}`：按批单号查批单。
- `GET /api/batches/{batch_no}/conflicts`：该批单号的晚到冲突记录。
- `POST /api/batches/{batch_no}/recover`：按批单号恢复入账。已入账批单为幂等空操作；缺失台账时补齐。

### 配额台账（乐观并发，先到生效）

- `GET /api/items/{id}/ledger`：查许可的配额台账。
- `POST /api/items/{id}/quota-adjustments`：额度调整，必须提交`expected_version`。版本冲突时返回409并携带`current_version`、`current_quantity`、`proposed_quantity`、`diff`，已入账数据不被覆盖。
- `GET /api/items/{id}/quota-adjustments`：调整历史（追加式台账）。

### 台账回填

- `POST /api/items/{id}/backfill-ledger`：按已批准许可回填单个缺失台账。
- `POST /api/backfill-ledger`：批量回填所有已批准但缺台账的许可。

批单接收、额度调整、回填和恢复均写入审计事件，与排污许可、台账历史串成可恢复的入账链。历史检查记录接口保持不变。

允许角色：applicant, inspector, compliance_manager, viewer。申报量超过许可量或检查发现高严重度问题时提高优先级；存在未关闭整改时不能批准。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
