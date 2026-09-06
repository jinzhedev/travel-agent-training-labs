# Agent 任务运行控制金标 v1

对应 [Lab 05](../../../labs/05-reliability-deployment/LAB.md)，共 12 条。重点是模型自主选择工具后，执行端能否守住整次任务预算，以及恢复是否沿用原候选动作与业务结果。

| Case | 覆盖 |
| --- | --- |
| L05-001–005 | 正常调用、一轮多调用、跨轮累计、切换工具、失败计费 |
| L05-006–007 | 工具参数不能修改任务预算，管理接口区分任务所有者 |
| L05-008–010 | 取消后的晚到动作、结果未知恢复、候选变化拒绝 |
| L05-011 | Context revision 变化后需要复核 |
| L05-012 | 多请求争用同一任务额度 |

在仓库根目录执行：

```bash
uv run python scripts/lab05_runtime.py eval --dry-run
uv run python scripts/lab05_runtime.py eval --output reports/local/lab05/runtime-report.json
uv run pytest tests/travel_core/test_lab05.py
```

HTTP 回放访问真实 Travel Core 服务，但模型动作为固定脚本，付费上游为模拟实现；报告明确标记 `scripted_actions` 与 `simulated`。L05-011 只在隔离测试中修改 revision，HTTP 回放记为 `not_run`，不改共享课堂数据。其余 11 条必须逐条通过，不能将跳过项计为通过。

`executed_calls` 统计持久化准入事件。额度在进入模拟付费执行前预留，失败不退还；进程中断可能留下已准入、无返回结果的调用。并发案例验证执行端计数，不能用来证明默认 Agent Strategy 并行执行工具。

真实模型的调用数量、停止行为、推荐质量和 A/B 成本变化另由 Dify/Phoenix 实验记录。本数据集不能替代这些验证。
