# Lab 06：Tool Calling + RAG 的持续改进

[操作步骤](LAB.md)从含查询模板的综合 Chatflow 开始，用 12 条固定案例和真实 Phoenix Experiment 完成：反馈关联、首次偏差、案例回流、Judge 复核、单变量修改、同集回归与发布判断。

- 前置：Lab 01 的天气、景点工具，Lab 03 的对象知识库，以及可访问的 Dify、Travel Core、Phoenix。
- 主结构：`Start → Agent → Template → Knowledge Retrieval → LLM → Answer`。
- 建议时间：150 分钟，搭建与冒烟约 25 分钟。
- 目录：`configs/local/lab06/` 保存每版参数、Prompt、DSL 及共用评测输入；`reports/local/lab06/` 保存运行报告和复核结果。
- 产物：两组版本与 Experiment、人工复核、门禁报告、发布观察与回退条件。

新 [12 条金标](../../datasets/eval/tool-rag-improvement-v1/README.md)只引用已有权威语料和工具事实。旧 [模拟生产数据](../../datasets/eval/continuous-improvement-v1/README.md)作为可选补充。

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare
uv run pytest tests/training_eval/test_lab06.py tests/scripts/test_run_phoenix_lab06_eval.py
```

数据校验和单元测试不代表真实 Dify 已通过。课堂 live 结果以实际 Dataset、Experiment、trace 和报告为准；线上灰度、业务结果与问题关闭需要另外的真实证据。
