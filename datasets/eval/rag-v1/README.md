# RAG 测评集 v1

这 12 道冻结题不是演示问题，而是 Day 2 AM 的质量门禁。它同时覆盖段落、表格、流程图、时序图、版本冲突、拒答、并行证据聚合和依赖型多跳。

## 三层证据标注

- Page：答案所在页，适合检查粗粒度召回。
- Object：段落、表格、流程图或时序图对象，适合检查解析与召回边界。
- Fine：表格单元格、流程边、时序事件，适合检查答案是否真的获得了必要关系。

`must_contain` 和 `must_not_contain` 只用于已实际生成答案的在线预测。离线检索模拟器不会伪造答案分数，报告中会明确写 `generation_status=not_run`。

## 使用

```bash
uv run python scripts/validate_rag_cases.py
uv run python scripts/run_offline_rag_eval.py --preset text-baseline
uv run python scripts/run_offline_rag_eval.py --preset object-hybrid
```

学员实验以相同的 12 道题、相同随机性设置和相同计时口径复跑。没有固定测评集，就不能把两次配置结果解释为改进。
