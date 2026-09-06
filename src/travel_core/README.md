# TravelCore

旅行计划、天气等业务 API，使用模拟数据。

## 部署方式

通过 docker 部署。

本地测试运行，在根目录下执行（运行在 8000 端口）：

```Shell
uv run fastapi dev src/travel_core/main.py
```

## 请求路径

```
HTTP JSON
  ↓
schemas.py：解析和校验
  ↓
services/*.py：执行业务逻辑
  ↓
models.py：读写数据库
  ↓
schemas.py：组织响应
  ↓
HTTP JSON
```

## 设计思路

Travel Core 的 HTTP 层只负责接收请求、注入请求上下文和组织响应；具体业务逻辑放在 `services/*.py` 中。这样 Dify 或其它调用方可以通过稳定的 API 调用业务能力，业务规则仍由 Travel Core 统一执行。

### 单个 API 与批量工具执行

普通业务代码已经明确知道要调用什么服务时，直接请求单个 API 更简单。例如，单独查询天气可以直接调用天气接口。

`POST /v1/tools:execute-batch` 面向 Agent 的工具运行时。Agent 在一轮决策中可能需要调用多个工具，例如同时查询天气和室内景点：

```json
{
  "calls": [
    {
      "name": "weather.forecast",
      "arguments": {
        "city": "厦门",
        "date": "2026-09-02"
      }
    },
    {
      "name": "poi.search",
      "arguments": {
        "city": "厦门",
        "tags": ["室内"]
      }
    }
  ],
  "execution_mode": "parallel",
  "response_mode": "detailed"
}
```

使用批量接口的主要原因是把这一轮工具调用作为一次运行处理：统一检查工具是否存在、参数是否有效、调用方是否有权限、是否需要人工确认，并统一返回每个工具的结果。多个互不依赖的只读查询可以使用 `parallel`，减少等待时间；默认的 `sequential` 按请求中的顺序执行。

批量接口不是所有业务 API 的替代品。可以按下面的规则选择：

| 场景 | 调用方式 |
| --- | --- |
| 应用代码调用一个明确的业务能力 | 直接请求单个 API |
| Agent 一轮需要调用多个独立工具 | `execute-batch` |
| 多个只读工具可以同时执行 | `execute-batch` + `parallel` |
| 工具之间有固定执行顺序 | `execute-batch` + `sequential` |
| 后一个工具必须使用前一个工具的动态结果 | 拆成多个工作流节点，或增加显式的数据流编排 |

当前 `calls` 中的参数在请求开始时就已经确定，因此 `sequential` 只保证执行顺序，并不会自动把前一个工具的结果注入后一个工具。涉及预订、提交等副作用的工具禁止并行；需要确认的工具必须提供 `approval_id`。

### 批量接口的请求边界

`execute_tools` 本身不直接实现工具规则，而是把执行委托给 `tools.execute_batch`，再由 `execute_idempotent` 包装一次可靠提交：

```text
HTTP 请求
  ↓
FastAPI / Pydantic 校验请求体
  ↓
request_context 注入租户、用户、权限、关联 ID 和幂等键
  ↓
execute_idempotent 检查重复请求
  ↓
tools.execute_batch 校验并执行工具
  ↓
保存响应并统一返回
```

批量执行要求请求带有 `Idempotency-Key`：

- 同一租户使用相同幂等键和相同请求内容时，直接重放已保存的响应；
- 同一幂等键对应不同请求内容时，返回 `409`，避免误用幂等键；
- 并发请求抢占同一幂等键时，只有一个请求执行，另一个请求收到冲突响应；
- 响应头 `X-Idempotent-Replay` 为 `false` 表示本次实际执行，为 `true` 表示返回了之前保存的结果。

这里的幂等保证覆盖 Travel Core 本地数据库记录和模拟工具执行。若工具将来调用外部订票、支付等系统，还需要外部系统支持幂等键，并配合 outbox 或 Saga 处理跨系统失败；本地数据库事务不能单独覆盖远程副作用。
