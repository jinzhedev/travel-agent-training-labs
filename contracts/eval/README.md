# 综合评测契约

CP01 Foundation、CP02 Tool Calling、CP03 Document RAG 和 CP04 Routing + HITL 分别在对应 dataset 中维护任务专用 case schema、预测格式和评分器。四类任务的责任层不同，不强行压成一个大而模糊的准确率。

CP04 已把证据工作形态、能力所有者集合、支持状态、执行器、fallback、自适应工作量、摘要覆盖、候选动作、人工介入、安全和耗时分层评分。跨可靠性、发布与四类任务的统一 run/report 契约仍为 `planned`，在 CP05–CP06 设计时再以现有报告为输入确定。
