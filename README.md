# 无线电频谱干扰调查与协调

模块化纯 Python 3.9.6+ 标准库项目，默认端口 `8332`。

模块结构：`app.py` 负责组装，`src/domain.py` 定义字段和错误，`src/rules.py` 负责评估、定位、授权和状态机，`src/repository.py` 管理 SQLite、版本和审计链，`src/service.py` 编排权限，`src/http_api.py` 提供接口，`src/audit.py` 生成审计哈希。

测量记录是事件基准的依据：事件创建时的强度只是初始基准，来源测量（`POST /api/items/<id>/sources`）会持续修正基准。来源类型与外部编号相同的重复回传只保留一条，强度不同时以观测时间更晚的为准，被替掉的旧值连同观测时间写入审计链（`source_replaced`）；强度相同的重复回传幂等忽略。更新已有来源需要在请求体中带 `expected_version`，两个监测站并发补录时后到的会收到 409 `version_conflict`，不会盖住先到的。基准一变，评估等级按新强度自动重算；若事件已经定位或进入处置，`payload.review_required` 会置为 true 提示复核，重新 `assess` 后清除。

跨区处置需要对方协调人授予代管授权（`delegate`，指定 `delegate_to`、`delegate_region` 和 `expires_at`），授权到期后再次办理会被拒绝并自动收回（审计记 `delegation_revoked`）。

```bash
python3 app.py --init --db ./data.db
python3 app.py --db ./data.db --port 8332
python3 -m unittest discover -s tests -v
```

使用 `X-User-Id`、`X-Role`、`X-Region` 请求头。接口为 `GET /health`、`GET /api/state`、`GET /api/items`、`GET /api/items/<id>`、`POST /api/items`、`POST /api/items/<id>/sources`、`POST /api/items/<id>/actions` 和 `GET /api/items/<id>/audit`。测试覆盖完整调查流程、测量更正、重复事件、跨区越权、定位置信度、版本冲突、测量基准驱动、来源去重与并发冲突、代管授权到期收回。协议接入、真实无线电传播模型和执法权限仍需由外部系统实现。
