# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、测量基准取舍、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。

## 接口

- `GET /health`、`GET /api/state`
- `POST /api/items` 创建干扰事件
- `POST /api/items/<id>/sources` 提交来源测量（支持重复回传）
- `POST /api/items/<id>/actions` 执行动作（`action` 字段指定）
- `GET /api/items/<id>/audit` 审计链
- `POST /api/delegations` 授予代管授权、`GET /api/delegations` 查询授权

## 测量记录如何成为事件依据

- 同一 `(source_type, external_id)` 的重复回传只保留一条当前记录：
  - 观测时间更晚且强度不同：替换为新记录（`outcome=superseded`）；
  - 观测时间更晚但强度一致：去重不落新内容（`outcome=ignored`，仍留审计）；
  - 观测时间更早：拒绝（409 `stale_measurement`）；
  - 观测时间相同的后到补录：拒绝（409 `simultaneous_measurement`），先到为准，并发提交由写事务保证。
- 被替换的旧强度连同旧观测时间、替换时间、替换人保存在 `source_revisions` 表和来源的 `revisions` 字段，并产生 `source_superseded` 审计事件。
- 事件基准测量在全部测量（创建测量、各来源当前测量、人工更正）中选取：**观测时间更晚的作准；观测时刻相同则先入库（先到达）的作准**。
- 基准强度变化时：若已经评估过，旧评估进入 `assessment_history`（连同失效时间和原因），按新强度重算等级；事件已定位或进入处置（`located/suspended/coordinating`）时置 `review_required` 提示，处理人执行 `acknowledge_review` 后留痕消除。事件版本号随之推进，旧版本写操作冲突失败。
- 动作 `correct_measurement` 为人为更正，立即确立基准，同样触发评估失效与复核提示。

## 区域与代管

- 除 `regulator` 外，请求头区域与事件区域不一致的写操作一律 403 拒绝。
- 协调人可通过 `POST /api/delegations` 给外区人员授予带 `expires_at` 的代管授权（区域级或针对单个事件）；授权到期后在下次访问时自动标记收回（`revoke_reason=expired`），其后越区操作重新被拒绝。

测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度、版本冲突、测量去重与替换留痕、基准重算与复核、同时刻并发补录、代管授权到期收回。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
