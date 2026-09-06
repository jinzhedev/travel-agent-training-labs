# 复杂文档 RAG 教学数据 v1

本目录提供 Day 2 AM 的冻结复杂 PDF、两种解析结果和对象级事实。内容为课程合成数据，借用厦门真实地名降低理解成本，不代表景点、轮渡或票务系统的实时规则。

## 文件

- `source/xiamen_travel_service_training_v1.pdf`：课程 PDF，包含普通文本、复杂表格，以及两页以扫描图像嵌入的流程图和系统交互图；页面文本提取不会自动得到图内关系。
- `source/source_spec.json`：PDF 的可审查源事实，也是生成页面与 gold 对象的依据。
- `parsed/text-only.jsonl`：从源规格生成的展平文本基线；保留页码，但不保留单元格定位、流程连接和时序关系。它不是实际运行 PDF 文本提取或 OCR 的输出。
- `parsed/object-aware.jsonl`：从源规格生成的对象级金标；保留 `page`、`bbox`、表格 `cells`、流程 `nodes/edges`、交互 `events` 和版本元数据，不代表某个解析器的实测能力。
- `manifest.json`：修订、来源、时效和模拟边界。
- `dify-import/`：由 JSONL 机械导出的 7 个页面文件和 14 个对象文件，用于 Dify 双知识库对照。

评测问题只维护在 `datasets/eval/rag-v1/cases.jsonl`。多跳证据计划只维护在 `datasets/rag/multihop/v1/plans.jsonl`，两处通过 `case_id` 关联。

## 修改与重新生成

正文、表格、图中节点与条件统一维护在 `source/source_spec.json`。`object_annotations` 保存实体和评测关注的细粒度证据 ID，不显示在 PDF 正文中。修改后在仓库根目录运行：

```bash
uv sync
uv run python scripts/generate_rag_document.py
uv run python scripts/validate_rag_cases.py
uv run pytest tests/scripts/test_generate_rag_document.py tests/training_eval/test_document_rag_eval.py tests/travel_core/test_rag.py
```

第一条生成命令会更新 PDF、两份 JSONL、Dify 导入 Markdown 和 manifest；不是保存 JSON 后后台自动同步。`scripts/export_rag_for_dify.py` 仅重导出已有 JSONL，不生成 PDF。不要手工修改生成物。

修改业务事实时同步检查评测问题与多跳计划；脚本不会自动改写期望答案。保持已有对象和证据 ID 稳定，新增或调整流程节点时还需检查生成脚本中的布局，并重新检查 PDF 版面。PDF 第 3、4 页继续以栅格图嵌入，保留图文解析的对照条件。

本地重新生成不会更新 Dify 中已导入的知识库，需要重新导入并完成索引。

## 证据层级

```text
document → page → object → cell / node / edge / event
```

纯文本基线可以命中相关页面，但无法证明找到了具体单元格、流程边或交互事件。对象级评测会分别报告 Page Recall、Object Recall 和 Fine-grained Recall，避免用“页面主题相关”代替“证据足以回答”。

## 边界

- PDF 和解析结果均为模拟数据，`is_simulated=true`。
- `observed_at` 与 `valid_until` 是教学冻结期，不表示真实运营规则。
- 离线检索器使用透明的中文字符/词项匹配和确定性重排模拟，只用于课程对照；真实 Dense、Hybrid、Rerank 和 TTFT 必须在课堂 Dify/模型环境中另行验证。
