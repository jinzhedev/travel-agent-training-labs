# Dify 知识库导入包

运行 `uv run python scripts/export_rag_for_dify.py` 生成两组 Markdown：

- `text-only/`：7 个整页文本文件，用作页面级基线；
- `object-aware/`：14 个对象文件，保留 object ID、page、bbox、table cell、flow edge 和 sequence event。

这两组文件由 `parsed/*.jsonl` 机械生成，JSONL 仍是唯一权威来源。Dify dataset ID、embedding 模型和 reranker 配置属于课堂环境，不能写死在 checkpoint DSL 中。
