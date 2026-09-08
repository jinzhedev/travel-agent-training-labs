# 多工具必要集合金标 v1

12 条任务、21 个独立轮次，用于 Lab 02 的候选检索与工具计划评测。运行入口为 `scripts/run_lab02_multi_tool_gold.py`，详细步骤见 [LAB-multi-tool-gold.md](../../../labs/02-tool-calling/LAB-multi-tool-gold.md)。

## 数据与边界

- `cases.jsonl`：任务级 `required_tools` 与 `rounds` 下的当前轮金标、冻结 Observation、禁止工具、依赖、参数和停止原因。
- `manifest.json`：参考日期、目录 revision、权限、环境、大风分支约定和召回阈值。
- `case.schema.json`、`prediction.schema.json`：与评分模块的 Pydantic 模型对应的 JSON Schema。
- `baseline.md`：本地候选与真实模型计划基线，保留失败结果。

`required_tools` 是所有可能业务分支的工具并集；当前轮次只看 `round_required_tools`。条件不成立时，整个任务不必调用并集中的所有工具。`max_calls` 是当前轮预算，不是跨轮总预算。

21 个轮次是独立快照，不按文件顺序串行执行。`wind_true` 与 `wind_false` 是互斥分支；模型均从原问题与该轮已提供的 Observation 开始。后续轮结果由人工冻结，不是上轮模型实际执行所得。这种评测能定位候选、动作、参数和依赖问题，不能证明完整 Observation Loop 或最终业务任务成功。

景点 ID、名称和标签与课程 POI 数据交叉校验。天气正反分支和退改结果是教学夹具，不代表实时天气、库存或真实订单。大风判断只采用 manifest 中的课堂约定。

固定 `allow_side_effects=false`，权限为 `travel.read`、`booking.read`。所有副作用工具都在每轮禁止集合内；部分轮次还禁止依赖未满足或条件不成立的后续工具。`forbidden_exposure_rounds` 因此包含提前暴露，并不等于出现危险写工具；副作用与越权暴露另列指标。

## 轮次覆盖

| 任务 | 轮次 | 检查内容 |
| --- | --- | --- |
| MT-001 | initial | 天气与室内景点两个独立目标 |
| MT-002 | initial | 酒店与景点两个独立目标 |
| MT-003 | initial、policy_free | 先查规则；结果返回后只回答下一步 |
| MT-004 | initial、wind_true、wind_false | 大风条件成立与不成立 |
| MT-005 | initial、found、empty | 无障碍景点存在与为空；草案参数来自结果 |
| MT-006 | initial、found、insufficient | 景点搜索后计算路线矩阵；不足两个时停止 |
| MT-007 | initial、waiting_weather、ready | 天气和景点均返回后才能生成草案 |
| MT-008 | initial | 已知对象只查一次详情 |
| MT-009 | initial | 查规则，不取消、不预订 |
| MT-010 | initial | 缺必要参数时澄清 |
| MT-011 | initial | 无需工具的回答 |
| MT-012 | initial | 拒绝无目的批量调用 |

## 预测文件

每行对应唯一的 `(case_id, round_id)`，必须覆盖全部 21 轮。缺行、重复行、额外轮次和缺失必要字段会报错。示例只说明格式，不能复制为基线：

```json
{"case_id":"MT-011","round_id":"initial","candidate_names":[],"allowed_tools":[],"action":"answer","calls":[],"stop_reason":"done","answer":"此处填写真实回答","source":"dify","run_id":"此处填写真实 workflow_run_id"}
```

- `candidate_names`：候选检索的实际结果。
- `allowed_tools`：实际送给模型的工具名，不可从金标生成，也不可超出候选集合。
- `calls`：当前轮实际计划，每项包含 `name` 和 `arguments`。不填写累计调用链。
- `source`：`model`、`dify` 或 `manual`。`manual` 用于人工记录实际运行，不代表模型基线。
- `run_id`：模型请求 ID 或 Dify 的 `workflow_run_id`。必须保留原始运行记录。
- `stop_reason`：`continue`、`done`、`needs_input` 或 `refused`。
- `error`：可选的运行错误类型；非空时该轮失败，不能用空预测跳过失败轮次。

评分器接受并行调用的任意顺序。当前没有等价替代工具，`acceptable_tools` 必须为空。参数按金标精确匹配；唯一额外兼容项是 `poi.get_details` 可显式传入 `city=厦门`。

报告中 `final_task_result=not_evaluated`，表示未执行工具、未评价最终回答的业务正确性。`passed=true` 只表示本实验的候选与当前轮计划达到标准。
