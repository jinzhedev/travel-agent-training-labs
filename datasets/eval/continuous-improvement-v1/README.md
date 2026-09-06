# Agent 持续改进冻结数据

修订：`continuous-improvement-v1.0.0`。

本目录提供 Day 3 PM 的离线课程证据，用来练习“线上 Trace／反馈 → 评审队列 → Eval 数据集 → 受控实验 → 发布判断 → 新证据回流”。所有记录均为课程模拟数据，`evidence_level` 为 `frozen_trace` 或 `offline_only`，不代表真实线上流量、真实用户反馈、真实模型对比或真实 Canary。

## 文件

| 文件 | 用途 |
| --- | --- |
| `production-traces.jsonl` | 13 条模拟生产 Trace，混合失败、成功、重复、敏感字段、延迟业务结果和 grader 分歧 |
| `cases.jsonl` | 每条 Trace 的课程金标：评审队列、首次偏差、隐私动作、数据集动作和目标 suite |
| `case.schema.json` | 金标契约 |
| `prediction.schema.json` | 学员策展判断契约 |
| `predictions.template.jsonl` | 空白预测模板 |
| `grader-calibration.jsonl` | 10 条人工金标与模拟 `judge-v1` 判定，用于计算总体和分 slice 一致性 |
| `release-candidates.jsonl` | 4 份模拟候选报告；包含单变量模型对比和不可归因的多变量候选 |
| `release-decision.schema.json` | 学员发布判断契约 |
| `release-decisions.template.jsonl` | 空白发布判断模板 |

## 课程自定义字段

`review_queue`、`failure_family`、`dataset_action` 和 `target_suite` 是本课程为了练习建立的分类，不是行业统一 taxonomy。生产系统应根据业务风险、现有 owner 和数据治理制度调整。

`retain_control` 表示保留一条成功样本，用来发现“修复失败样本后破坏原有能力”；`link_duplicate` 表示只记录与权威 case 的关联，不重复增加权重；`do_not_promote` 表示当前证据不足以进入回归集。

## 证据边界

- 原始 Trace 不等于金标；用户反馈、自动 grader、业务 outcome 和人工意见可能互相冲突。
- `production-traces.jsonl` 中的邮箱使用 `.test` 保留域名，仍要求学员按敏感字段处理。
- release 指标是课程模拟结果，不能用于判断 DeepSeek V4 Flash 或 GLM 5.3 的真实优劣。
- 本数据集只验证策展和发布判断方法，不训练或调用 LLM judge。
