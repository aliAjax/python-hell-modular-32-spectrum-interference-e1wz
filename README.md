# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和 `GET /api/items/<id>/audit`。根路径 `/` 提供可操作页面，可创建事件、提交现场复测、查看结案条件与审计链。

结案规则（动作 `resolve`）：协调员不能再凭勾选结案，必须有现场人员通过 `retest` 动作提交的复测证据（`strength_dbm`、`measured_at`、`location`，区域默认取 `X-Region`）。只有**停用授权签发之后**、**与事件同一区域**的复测可作为依据，且以其中**测量时间最新的一条**为准：强度不高于 `-80 dBm`（`rules.CLEAR_STRENGTH_DBM`）才允许结案。跨区域或早于授权的复测只进入复测历史并标注排除原因。

结案后若补录测量时间更晚的同区超标复测，事件自动从 `resolved` 回到 `coordinating`，原结案记录整体移入 `payload.resolution_history` 并标记 `superseded_by_retest_seq`，审计链中保留翻案事件；晚到的旧结果只进入复测历史，不改变当前结论。记录详情中的 `closure` 字段给出实时结案判定，`retests` 字段给出每条复测是否计入。

测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度、版本冲突，以及复测驱动结案、跨区/授权前复测排除、结案后翻案与旧结果补录。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
