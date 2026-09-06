# Lab 06：工具、文档与最终建议的回归集

12 条课程编写的案例，用于 `Start → Agent → Template → Knowledge Retrieval → LLM → Answer`。覆盖轮椅用户、开放日、只到游客中心、普通用户、只问天气、缺少日期、资料不足、只读能力边界和指令冲突。

`cases.jsonl` 是本 Lab 评测问题的权威来源。每条包含原始问题、切片、文档对象 ID、天气日期、工具复核要求和最终任务验收条件。这里不复制 POI 或文档 fixture：准备脚本从 `datasets/rag/document-ai/v1/parsed/object-aware.jsonl` 取得原文，从 Travel Core 天气实现取得冻结参考，同时记录相关源码、语料、POI 和目录的 SHA-256。

所有文旅内容都是课程模拟事实。真实调用 Dify 只能证明应用在这些冻结条件下的表现；天气参考不是实时预报，文档不是出行指南。

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare
uv run python scripts/run_phoenix_lab06_eval.py prepare --apply
```

第一条只检查本地案例与引用。第二条创建 Phoenix Dataset 并返回版本 ID；相同名称已有不同内容时停止，需改用新版名称。对照实验必须显式指定同一版本。

`document_ids` 是必须取得的证据。空数组表示本例不要求特定对象，不能据此认定拒答、澄清或任务行为已经通过。工具检查依据原始 trace，不能从 Agent 查询摘要反推工具已经正确执行。

新增回流案例时，在本地复制案例集再追加，保留 `source_trace_id`、脱敏后的问题、验收条件和来源；重复问题只关联原案例。用新 Dataset 名称准备下一版，再让基线和候选都运行新版。不可把模型自己的回答直接保存成参考答案。

操作步骤见 [Lab 06](../../../labs/06-evals-release/LAB.md)。
