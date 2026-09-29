# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权、复测判定和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/retests`、`POST /api/items/<id>/actions` 和 `GET /api/items/<id>/audit`。

## 复测与结案规则

现场人员（`analyst`/`monitor`/`field_operator`）通过 `POST /api/items/<id>/retests` 提交复测，字段为 `strength_dbm`、`measured_at`、`location`（可选 `note`），区域自动归属事件区域，跨区域提交返回 `region_mismatch`。事件处于 `located`/`suspended`/`coordinating`/`resolved` 状态时可提交。

结案（`resolve`）不再接受请求方自报的 `measurement_cleared`，而是由系统按 `GET /api/items/<id>` 返回的 `resolve_readiness` 判断：

1. 已签发停用授权（`suspend` 时记录 `suspend_authorized_at`，可用 `authorized_at` 指定）；
2. 存在**授权时间之后**且**与事件同一区域**的复测；
3. 其中**最新一条**复测强度不高于消除阈值 `rules.CLEARANCE_STRENGTH_DBM`（-80 dBm）为达标；最新复测超标则返回 `interference_present`，无法结案。

已结案事件补录一条授权之后、同区域且时间更晚的超标复测时，事件自动回到 `coordinating`（处置中），审计记录 `reopen`，原结案记录保留在 `payload.resolution` 与 `payload.resolution_history` 中；达标复测或晚到的更早结果只进入复测历史，不改变当前结论。事件详情中的 `resolve_readiness` 同时给出能否结案、原因和作为依据的最新复测。

测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度、版本冲突，以及复测驱动结案、超标复测拦截、结案后超标补录重开、晚到旧结果不改变结论等场景。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。

