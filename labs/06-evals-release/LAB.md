# Lab 06：Tool Calling + RAG 的持续改进

先搭建最小综合 Chatflow，再围绕轮椅用户的请求完成一次改进：运行基线，关联反馈与 trace，定位首次偏差，把问题加入评测集，只修改一个变量，检查回归结果，最后给出发布与观察条件。

本 Lab 使用已有的两个只读工具和 Lab 03 知识库。

## 完成标准

- 跑通 `Start → Agent → Template → Knowledge Retrieval → LLM → Answer`。
- 使用 12 条金标运行基线和候选，保存每条 trial 的回答、耗时、评分与 trace 关联信息。
- 区分工具错误、检索缺失、证据交接错误和证据齐全后的决策错误。
- 复核 Judge 与人工的分歧，只修改一项 Prompt 规则或最终回答模型。
- 保留成功对照，检查关键人群、只读能力和耗时是否退化。
- 冻结基线、候选及 Judge 后，用 `secret` 数据集完成最终测试，单独记录结果。
- 输出发布判断、观察窗口、停止与回退条件；没有真实线上证据时保持问题未关闭。

## 准备工作

先完成 [平台环境](../../README.md)、Lab 01 的两个工具配置，以及 Lab 03 的 `XM-Guide-Object` 知识库。

在课程根目录执行：

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare
```

预期得到 12 个 `L06-*` 案例 ID。该命令只检查本地数据与引用，不调用 Dify、模型或 Phoenix，不产生实验结果。

这 12 条是开发集。另有 `datasets/eval/secret/`，留到实验 6 的最终测试再打开和运行。课堂版文件随仓库提供，没有访问隔离；提前不看、不用于调整应用或 Judge 是练习约定，不能把它当作严格保密的盲测。

材料：

| 材料                                                                    | 用途                                             |
| ----------------------------------------------------------------------- | ------------------------------------------------ |
| [12 条金标](../../datasets/eval/tool-rag-improvement-v1/cases.jsonl)    | 原始问题、切片、证据引用、工具检查与任务验收条件 |
| [Agent Prompt](prompts/agent.txt)                                       | 查询天气和候选，保留原始约束，输出待检索信息     |
| [基线回答 Prompt](prompts/answer-v0.txt)                                | 根据两类信息生成建议                             |
| [候选回答 Prompt](prompts/answer-v1.txt)                                | 在基线上增加一段约束核对规则                     |
| [Judge Prompt](prompts/judge-v1.txt)                                    | 评估最终建议，允许`pass / fail / review`         |
| [发布记录模板](release.template.json)                                   | 固定模型、Prompt、知识库、工具及运行预算         |
| [模拟生产记录](../../datasets/eval/continuous-improvement-v1/README.md) | 延迟投诉、评分冲突、安全事件和发布判断补充练习   |

建议用时：搭建与冒烟 25 分钟，基线 20 分钟，分诊与回流 20 分钟，Judge 复核 15 分钟，单变量修改与回归 35 分钟，门禁 20 分钟，发布观察与整理 15 分钟，共 150 分钟。这是操作与分析预算，模型排队和评分等待需另外估算。

课前用冒烟样本测量实际耗时。若每题 3 次的完整实验超出课堂时间，两版统一使用 `--repetitions 1`，现场完成全部 12 题；课后再将两版都补跑为每题 3 次的新实验。一次 trial 只能用于初步比较，不能证明重复运行稳定；不得混用不同 trial 数的报告。

## 实验 1：最小综合 Chatflow

### 1. 创建应用

新建 Chatflow，命名为 `Lab 06`，在监测中启用 Phoenix，项目名也填 `Lab 06`。连接以下节点：

```text
Start
  → Agent（查询天气与候选景点）
  → Template（整理检索查询，不调用模型）
  → Knowledge Retrieval（核对候选与用户指定景点）
  → LLM（生成建议）
  → Answer
```

所有模型节点关闭 Memory。每条评测请求使用新会话，防止前一题的回答进入下一题。

### 2. Agent 节点

- Agent Strategy：`FunctionCalling`。
- 使用 Lab 01 模型 + 完整模型配置。
- Tool List 只启用 `weather_forecast`、`poi_search`。可从已配置的工具中选取，无需重新导入整个目录。
- Query 绑定 `sys.query`，Context 留空。
- Maximum iterations：先设为 `4`。两类查询可能分两轮执行，取得结果后还需要一轮输出摘要。
- Instruction 粘贴 [Agent Prompt](prompts/agent.txt)。

两个工具的日期字段不同：

| 工具               | 日期字段                 | 本次输入为 2026 年 9 月 9 日时 |
| ------------------ | ------------------------ | ------------------------------ |
| `weather_forecast` | `start_date`、`end_date` | 都是`2026-09-09`               |
| `poi_search`       | `date`                   | `2026-09-09`                   |

### 3. Template 节点

在 Agent 与知识检索之间增加“模板转换 / Template”节点，命名为 `整理检索查询`，配置两个输入：

| 变量名       | 绑定         |
| ------------ | ------------ |
| `query`      | `sys.query`  |
| `agent_text` | `Agent.text` |

粘贴 [query.jinja](prompts/query.jinja)：

```jinja
用户要求：{{ query }}
核对事项：{% if "待检索：" in agent_text %}{{ agent_text.rsplit("待检索：", 1)[-1] }}{% else %}{{ query }}{% endif %}
```

模板保留原始要求，只追加 agent 输出中的待检索事项。

### 4. Knowledge Retrieval 节点

- 知识库选择 Lab 03 的 `XM-Guide-Object`。
- Query 绑定 `整理检索查询.output`
- 采用 Lab 03 已验证的检索配置；若尚未确定，可先从语义检索、Top-K `6`、阈值 `0.50`、Reranker 关闭开始。
- 确认节点设置与知识库设置各自的作用，记录本节点实际参数。

### 5. 最终 LLM 节点

命名为 `生成建议`。将知识检索的 `result` 绑定到 LLM Context，System Prompt 粘贴 [answer-v0.txt](prompts/answer-v0.txt)。在 System Prompt 末尾通过变量选择器插入 Context：

```text
检索文档：
<绑定的 Context>
```

User Prompt 填写以下结构，其中两个位置都使用 Dify 变量选择器：

```text
用户原始问题：
<sys.query>

工具查询摘要：
<Agent.text>
```

最终模型同时读取原始要求、工具摘要和检索证据。

Answer 只绑定 `生成建议.text`，不增加润色节点。在应用功能中开启“引用和归属 / Citation and Attribution”（默认开启-会在引用知识库的标注-在 Lab 03 里已经介绍过-关闭这个开关就不会显示了），便于 API 返回检索资源。

### 6. 冒烟测试

发布应用，输入：

```text
2026 年 9 月 10 号去厦门，同行者全程坐轮椅。想去植物园，也接受其他景点，请结合天气给出建议。
```

按顺序打开 Dify 节点 trace：

1. 天气请求的城市与起止日期是否正确，工具是否返回了结果。
2. 景点查询是否取得候选；“查询完成”和“推荐合适”分开记录。
3. Agent 摘要及模板输出是否保留用户约束、指定景点和实际候选。
4. 检索是否取得 `XM-ACCESS-001-P5-VENUES` 和 `XM-GUIDE-001-P1-VENUE-TABLE`。
5. 两类证据是否实际进入最终 LLM 的输入。
6. 最终建议是否区分植物园游客中心与整个园区，并引用实际取得的对象。

再运行两条成功对照：

```text
2026 年 9 月 10 号去厦门，我坐轮椅，只到植物园游客中心，不游览园区。查一下天气和景点资料，这样是否有无障碍设施，最晚几点进去？
```

```text
只查 2026 年 9 月 10 日厦门天气，告诉我会不会下雨，不需要景点推荐。
```

后一条不应调用景点工具。固定的知识检索节点仍会运行，因此本结构不是按需路由的最短路径；记录其耗时即可，本实验先固定结构研究改进效果。

如果工具不能连接、节点变量为空或 Phoenix 中找不到对应运行，先修复环境与绑定。业务答案不正确可以作为基线失败；不要通过改金标把它判成成功。

## 实验 2：冻结条件并运行基线

### 1. 环境变量与版本记录

在本地 `.env` 中填写：

```env
DIFY_LAB06_API_KEY=<Lab 06 已发布 Chatflow 的 API key>
```

在仓库根目录执行：

```bash
mkdir -p configs/local/lab06/baseline/prompts configs/local/lab06/evaluators configs/local/lab06/datasets reports/local/lab06
cp -n labs/06-evals-release/release.template.json configs/local/lab06/baseline/manifest.json
cp -n labs/06-evals-release/prompts/agent.txt labs/06-evals-release/prompts/answer-v0.txt labs/06-evals-release/prompts/query.jinja configs/local/lab06/baseline/prompts/
cp -n labs/06-evals-release/prompts/judge-v1.txt configs/local/lab06/evaluators/judge-v1.txt
```

目录按用途组织：

```text
labs/06-evals-release/             # 课程提供的 Prompt 和配置模板
configs/local/lab06/
  baseline/                       # 基线的 manifest.json、prompts/、workflow.dify.yml
  candidate/                      # 候选的 manifest.json、prompts/、workflow.dify.yml
  evaluators/                     # 两版共用的 Judge，修订时另存文件
  datasets/                       # 本地新增的回流案例集
reports/local/lab06/               # 运行报告、请求日志、人工复核和发布判断
```

以上复制命令不会覆盖已有文件。已经填写过旧路径 `reports/local/lab06/baseline.manifest.json` 的，先将其移到 `configs/local/lab06/baseline/manifest.json`，再执行初始化；目标已有文件时先核对内容，不覆盖。

编辑基线 manifest，填完占位值，记录节点实际参数。将基线 `prompts/` 中的内容与 Dify 实际配置核对；如果搭建时改过 Prompt，把修改同步到副本，包括最终 LLM 的 User Prompt（可另存 `prompts/answer-user.txt`）。变量绑定以 DSL 为准。

将已发布基线的 DSL 导出为 `configs/local/lab06/baseline/workflow.dify.yml`，并在同目录记录发布日期。开始评测后保留本版文件，后续修改在候选目录进行。`configs/local/` 和 `reports/local/` 都是本地目录，不提交；凭据继续放在 `.env`。

脚本读取 manifest 记录配置，不会把同目录的 Prompt 自动发布到 Dify。修改本地文件后，仍需在 Dify 更新并发布，再保存对应 DSL。

### 2. 创建 Phoenix Dataset

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare --apply
```

脚本把 12 条问题、验收要求和引用的冻结事实写入 `lab06-tool-rag-v1`，返回 Dataset ID 与版本 ID。记录 `dataset_version`，后面命令替换 `<版本 ID>`。

来源直接取自课程准备好的（模拟）文档对象与天气实现结果。

### 3. 基线实验

先检查本地参数：

```bash
uv run python scripts/run_phoenix_lab06_eval.py run \
  --dataset-version '<版本 ID>' \
  --manifest configs/local/lab06/baseline/manifest.json \
  --output reports/local/lab06/baseline.json \
  --with-llm-judge --judge-prompt configs/local/lab06/evaluators/judge-v1.txt --dry-run
```

`--dry-run` 只做本地预检，不调用模型。去掉该参数才实际运行：

```bash
uv run python scripts/run_phoenix_lab06_eval.py run \
  --dataset-version '<版本 ID>' \
  --manifest configs/local/lab06/baseline/manifest.json \
  --output reports/local/lab06/baseline.json \
  --with-llm-judge --judge-prompt configs/local/lab06/evaluators/judge-v1.txt
```

默认每题 3 次，共 36 次 Chatflow 请求，Judge 在请求完成后评分。评分默认并发 4 项，可在 `run` 或 `rejudge` 命令中增加 `--eval-concurrency 4` 调整；设为 `1` 则串行评分。Dify 请求仍串行执行。遇到评分服务限流时降低并发数。不要同时编辑或发布应用。工具、检索与生成调用来自真实 Dify。

在 Phoenix 的 Dataset → Experiments 中打开 `lab06-baseline`，查看：

| 评分或记录                | 能检查什么                     | 还需要什么                                     |
| ------------------------- | ------------------------------ | ---------------------------------------------- |
| `response_available`      | API 是否成功返回非空回答       | 非空不等于任务完成                             |
| `evidence_coverage`       | API 资源是否包含所需对象       | 资源缺失或不完整时核对检索节点                 |
| `citation_sources`        | 答案对象 ID 是否来自 API 资源  | 原文是否支持结论仍需语义判断                   |
| `task_constraints_<版本>` | 最终建议是否满足金标约束       | Judge 分歧需人工复核                           |
| `elapsed_s`               | 整次 Chatflow HTTP 请求耗时    | 不包含后续 Judge 和 trace 查询，不是 TTFT      |
| `api_reported_usage`      | Dify API 实际返回的用量        | 缺失不按零处理；未验证聚合口径前不称为全链成本 |
| `dify_trace_id`           | 用新会话 ID 关联到的原始 trace | 未匹配时按 conversation ID 人工定位            |

`review` 表示需要复核，不能改写成 pass。超时和错误保留在请求日志与失败分母里，不能只统计成功响应。

若运行中断，先查看 `baseline.requests.jsonl`、Phoenix Experiment 和本地报告状态。报告已有 `experiment_id` 和 `task_runs` 时，应用结果已经保存，可续跑缺失或执行失败的评分：

```bash
uv run python scripts/run_phoenix_lab06_eval.py resume \
  --report reports/local/lab06/baseline.json \
  --output reports/local/lab06/baseline-resumed.json \
  --judge-prompt configs/local/lab06/evaluators/judge-v1.txt \
  --eval-concurrency 1 --eval-timeout 600
```

先结束原评分进程，再执行续跑；把 `--report` 换成实际报告路径。`resume` 沿用原 Experiment，只补缺失或执行失败的评分，不重新调用 Dify。已有 `fail`、`review`、`not_applicable` 都属于已完成评分，不会重评。代码评分器、Judge prompt、模型和接口地址须与原实验一致；更换 Judge 配置使用 `rejudge`，可用 `--eval-timeout 600` 调整超时。

续跑结束后，查看输出的 `pending_evaluations`：为 `0` 表示评分齐全；大于 `0` 表示仍有缺失或失败，可再次续跑并指定新的输出文件名。后续分析使用续跑生成的报告。若原报告还没有 `experiment_id` 和 `task_runs`，先核对请求日志及 Phoenix 已完成范围，不能用 `resume` 恢复应用调用。

若已更换 Judge 配置，又需要补齐规则评分，在 `resume` 命令中增加 `--allow-judge-change`。脚本按当前配置生成 Judge 版本名，补齐该版本对全部已保存回答的评分，保留旧版本评分；规则评分仍只补缺失或执行失败项。新报告的 `judge_revision` 会更新，后续版本比较须使用同一 Judge 版本。

DeepSeek V4 Judge 默认关闭思考模式，以兼容分类评分所需的指定函数调用；该调用参数计入 Judge 版本。Phoenix 请求直连配置的地址，不使用环境变量中的 HTTP 代理；读取报告的默认 HTTP 超时为 30 秒，与 `--eval-timeout` 指定的评分执行超时分开。若评分执行结束后，在读取结果时中断，已写入 Phoenix 的评分仍会保留；使用新输出文件名再次执行 `resume`，可补齐剩余项并保存报告。

## 实验 3：从反馈定位偏差并回流案例

### 1. 关联同一次运行

选择 L06-001 或其同义表达 L06-002，记录：

| 字段                                                       | 记录 |
| ---------------------------------------------------------- | ---- |
| 应用版本、manifest、Experiment ID                          |      |
| case ID、trial、message ID、conversation ID、原始 trace ID |      |
| 用户原始要求                                               |      |
| 工具参数与结果                                             |      |
| 检索原文与最终 LLM 的输入                                  |      |
| 最终建议、Judge 判断                                       |      |
| 人工依据、首次偏差或未知项                                 |      |

对自己的输出做人工场景核验：如果用户按建议游览，是否仍能满足其“全程坐轮椅”的要求？

### 2. 分诊与修改对象

| 观察到的证据                                  | 优先检查                   | 建议的回归验证集             |
| --------------------------------------------- | -------------------------- | ---------------------------- |
| 天气或景点日期错误、调用了不需要的工具        | Agent 指令、参数和工具调用 | Tool 专项，并保留端到端 case |
| 原话有约束，Agent 摘要或检索 Query 丢失了约束 | 节点交接                   | Context／端到端              |
| 检索未取得设施限制，或读了错误文档            | Query、知识库与检索配置    | RAG 专项，并保留端到端 case  |
| 原始证据齐全并已进入最终模型，仍承诺全园可达  | 最终决策 Prompt 或模型     | Decision／端到端             |
| 允许只到游客中心，Judge 却按全园请求判错      | 评分标准与人工解释         | Judge 校准                   |
| 只读应用声称已经预订成功                      | 能力边界与答案真实性       | 安全／端到端                 |

按执行顺序检查 trace，定位最早可观察到的偏差，再从该处追查原因。只需注意，_首次偏差不一定是根因_。

### 3. 新案例与成功对照

复制案例文件到 `configs/local/lab06/datasets/cases-v2.jsonl`，准备后续回流版本：

```bash
cp datasets/eval/tool-rag-improvement-v1/cases.jsonl configs/local/lab06/datasets/cases-v2.jsonl
```

从自己的真实运行中选择尚未被覆盖的失败或边界表达，人工核对参考要求，追加唯一 case ID、原始问题、`source_trace_id`、切片与证据对象。先去掉个人信息。若只是 L06-001 的重复表现，记录关联与频次，不重复加权。

保留 L06-004、005、008、010 等成功对照。它们用于检查是否把“谨慎处理轮椅限制”修成了“拒绝所有出行建议”。原 trace 的模型回答不能直接作为金标。

本轮单变量比较继续使用已冻结的 v1。新问题形成下一轮：

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare \
  --cases configs/local/lab06/datasets/cases-v2.jsonl \
  --dataset-name lab06-tool-rag-v2 --apply
```

下一轮基线和候选都要使用新 Dataset 的名称与版本重跑。

## （扩展）实验 4： 人工复核打分器（Judge）

在 12 条基线样本中，每条先选择第 1 次 trial，独立标注人工 `pass / fail / review`、理由和证据，再看 Judge 的评分。对轮椅、缺少日期、游客中心、只读能力等分歧，继续查看其余 trial。

记录：

| case / trial | 人工 | Judge | 证据与分歧原因 | 采用的结论 |
| ------------ | ---- | ----- | -------------- | ---------- |
|              |      |       |                |            |

根据这个记录，计算出有明确判断（非‘review‘）的样本的一致率（人工 = 自动），同时单独列出 review 的数量。
误放行率，是人工 fail 但 Judge pass 的比例；误拒绝率，是人工 pass、Judge fail 的比例。
要注意的是，如果没有对应的正例/负例，不能判断相应方向上的指标。--比如，假设只有正样本，且人工和自动打分器都是 pass，那就无法说明打分器是否有能力识别出错误（一个永远输出 pass 的打分器也能做到，但完全没用）。

需要修订时，在 `configs/local/lab06/evaluators/` 中复制 `judge-v1.txt` 为 `judge-v2.txt`，只修改错误的判定规则，保留输入字段。对已保存的回答重新评分：

```bash
uv run python scripts/run_phoenix_lab06_eval.py rejudge \
  --report reports/local/lab06/baseline.json \
  --judge-prompt configs/local/lab06/evaluators/judge-v2.txt \
  --output reports/local/lab06/baseline.rejudged.json
```

该命令只调用 Judge，不重新执行 Dify。新评分器名称里会有一个（包含了 Prompt、服务入口和模型的）哈希值，旧评分会保留。

有限样本上的修订只说明当前分歧得到处理。实验 6 冻结配置后，再用没有参与调整的 `secret` 案例及人工复核验证；不要提前用它挑选 Judge。比较基线与候选时，两版都使用最终确定的同一 Judge。

## 实验 5：单变量修改与回归

### 1. 提出假设并设计改进实验

例：如确认必要证据已经传入最终模型但仍然答错，则可以有以下的假设及实验：

```text
目标：减少轮椅用户请求中“局部设施可达被当成全程可达”的错误。
变量：最终 LLM 的答案提示词，或该节点的模型，二选一。
固定：其他（Agent 模型与指令、查询模板、工具、知识库、检索参数、Memory、Dataset 版本、Judge、trial 数、预算）。
失败条件：原问题没有改善；成功对照退化；出现事实或能力边界错误；耗时超过预定预算。
```

### 2. 选择候选

二选一：

- **Prompt**：把最终节点 System Prompt 的基础文本从 `answer-v0.txt` 换为 `answer-v1.txt`（两文件只差一段约束核对规则）。
- **模型**：只更换最终回答模型（Agent 节点的模型保持不变）。记录服务商、模型名、可获得的版本和参数。如果换模型必须改其他配置（如 Deepseek 不支持名字中带`.`的工具，所以需要改工具名），需要同时标记。

基线已经全部通过时，不保证候选能更好。可以比较*重复运行的稳定性和延迟*，结果没有改善就保留基线。

复制基线的参数和 Prompt，建立候选目录：

```bash
mkdir -p configs/local/lab06/candidate/prompts
cp -n configs/local/lab06/baseline/manifest.json configs/local/lab06/candidate/manifest.json
cp -n configs/local/lab06/baseline/prompts/* configs/local/lab06/candidate/prompts/
```

把 manifest.json 里的 `release_id` 改为 `lab06-prompt-v1` 或 `lab06-model-b`（会作为 Experiment 的名称），仅改变对应的 `answer_prompt_revision` 或 `answer_model`。如果是改 Prompt 候选，把课程 `answer-v1.txt` 复制到候选 `prompts/`，移除候选目录中不再使用的 `answer-v0.txt`，并同步到 Dify。发布候选并将 DSL 导出为 `configs/local/lab06/candidate/workflow.dify.yml`。

### 3. 同集复跑

```bash
uv run python scripts/run_phoenix_lab06_eval.py run \
  --dataset-version '<与基线相同的版本 ID>' \
  --manifest configs/local/lab06/candidate/manifest.json \
  --output reports/local/lab06/candidate.json \
  --with-llm-judge --judge-prompt configs/local/lab06/evaluators/judge-v1.txt
```

若上一节修订了 Judge，命令中的 Judge 路径替换为 `configs/local/lab06/evaluators/judge-v2.txt`，比较时使用 `baseline.rejudged.json`。

在 Phoenix 并排比较两组 Experiment。逐条记录原失败是否改善、原成功是否退化；按 `wheelchair`、`missing_date`、`success_control`、`capability_boundary` 等切片汇总，不能只看总平均分。

对每次 trial 检查整次请求耗时、实际工具调用次数和迭代终态。36 次请求的 p95 是课堂样本描述，不能当作生产容量结论。用量与费用字段缺失时写 `not_observed`；尚未核对全链聚合口径时，不用最终节点 Token 代替完整任务成本。

## 实验 6：CI 门禁与版本判断

### 1. 完成人工复核

复制脚本生成的候选复核模板：

```bash
cp reports/local/lab06/candidate.reviews.template.jsonl reports/local/lab06/candidate.reviews.jsonl
```

逐条填写候选的全部 trial（默认 36 次；两版统一使用 `--repetitions 1` 时为 12 次）：

- `task_result`：最终建议是否满足金标，填 `pass / fail / pending`。
- `tool_result`：依据原始工具 trace 核对金标中的 `tool_check`；缺少日期且正确没有调用工具，可以填 pass。
- `critical_violation`：是否违反明确硬约束、编造操作成功或发生异常写操作；查证后填 `true / false`，未查证保留 `null`。
- `reason`：人工结论依据；如果覆盖了 Judge 或代码诊断结论，说明原因。
- `trace_id`：本次 Dify 原始 Phoenix trace ID；没有关联成功时先补查，不猜 ID。

核对原始检索 Context 与引用的语义支持。`task_result` 应包含这一判断，不能仅因为代码评分命中了对象 ID 就填 pass。

### 2. 执行检查

```bash
uv run python scripts/run_phoenix_lab06_eval.py check-release \
  --baseline reports/local/lab06/baseline.json \
  --candidate reports/local/lab06/candidate.json \
  --reviews reports/local/lab06/candidate.reviews.jsonl \
  --output reports/local/lab06/release-check.json
```

使用修订后的基线报告时，相应替换 `--baseline` 路径。

| 退出码 | 状态     | 含义                                               |
| ------ | -------- | -------------------------------------------------- |
| `0`    | `pass`   | 课堂只读候选通过当前固定集与人工复核，进入发布判断 |
| `1`    | `block`  | 任务、工具、关键约束或耗时预算失败                 |
| `2`    | `review` | 条件不可比、trial 不完整、证据缺失或人工复核未完成 |

检查会核对 Dataset 版本、事实来源哈希、评分器、trial 数和单变量范围。复核必须与全部候选 run ID 一一对应。失败与待定都不能用更高平均分抵消。

这条本地命令可作为 CI 的评测检查步骤，但没有替你配置远程 CI。依据课件正文填写触发规则：Prompt、回答模型、工具 Schema、RAG 数据、评分器变更分别触发哪些评测；定时检查还要覆盖未提交代码但外部依赖变化的情况。

课程应用只有查询能力。此处 pass 不证明预订、支付、幂等或跨租户安全已验证；这些问题继续使用已有专项证据。无需为本 Lab 新增写工具。

### 3. secret 最终测试

公开开发集完成回归及门禁后，冻结基线、候选的 DSL、Prompt、manifest 和最终 Judge 配置，再运行 `secret`。这是 8 条课堂模拟案例的最终检查；不在看到测试结果后继续挑选本轮候选。

准备独立 Dataset：

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare \
  --cases datasets/eval/secret/cases.jsonl \
  --dataset-name secret --apply
```

记录输出的 `dataset_version`，替换下面两个命令中的 `<secret 版本 ID>`。这里不能填写公开开发集的版本 ID。完整数据说明见 [secret README](../../datasets/eval/secret/README.md)。

先在 Dify 中恢复并发布已冻结的**基线配置**，核对 `.env` 的 `DIFY_LAB06_API_KEY` 指向该应用。manifest 只记录配置，不会替你切换 Dify。执行：

```bash
uv run python scripts/run_phoenix_lab06_eval.py run \
  --dataset-name secret --dataset-version '<secret 版本 ID>' \
  --manifest configs/local/lab06/baseline/manifest.json \
  --output reports/local/lab06/secret/baseline.json \
  --repetitions 1 --eval-concurrency 1 \
  --with-llm-judge --judge-prompt configs/local/lab06/evaluators/judge-v1.txt
```

再恢复并发布已冻结的**候选配置**，核对应用 API key，然后执行：

```bash
uv run python scripts/run_phoenix_lab06_eval.py run \
  --dataset-name secret --dataset-version '<secret 版本 ID>' \
  --manifest configs/local/lab06/candidate/manifest.json \
  --output reports/local/lab06/secret/candidate.json \
  --repetitions 1 --eval-concurrency 1 \
  --with-llm-judge --judge-prompt configs/local/lab06/evaluators/judge-v1.txt
```

两条命令的 Judge 路径及 `.env` 中的 Judge 配置必须相同。若已采用 `judge-v2.txt`，两处都替换。默认在本步骤每题跑一次，两版合计 16 次 Dify 调用；需要检查重复运行时，两版都将 `--repetitions` 改成 `3`，合计 48 次。即使开发集每题跑三次，本步骤也可统一跑一次，但不能把开发集报告与 secret 报告互相配对比较。

在 Phoenix 的 `secret` Dataset 下比较基线与候选 Experiment；同名实验通过 Dataset 和 Experiment ID 区分。对 secret 候选的全部 8 条 trial 填写人工复核；若每题三次则为 24 条。沿用本实验第 1 节的字段要求，并与当前 Judge 对照，记录分歧和待定项：

```bash
cp reports/local/lab06/secret/candidate.reviews.template.jsonl \
  reports/local/lab06/secret/candidate.reviews.jsonl
```

填写完成后单独检查 secret 结果：

```bash
uv run python scripts/run_phoenix_lab06_eval.py check-release \
  --baseline reports/local/lab06/secret/baseline.json \
  --candidate reports/local/lab06/secret/candidate.json \
  --reviews reports/local/lab06/secret/candidate.reviews.jsonl \
  --output reports/local/lab06/secret/release-check.json
```

如果评分中断，用前述 `resume` 命令并换成 secret 报告路径；后续检查也使用评分补齐后的报告。若改了 Judge，基线与候选均须采用相同新版本，并记录本次测试条件变化。

将公开开发集和 secret 的结果分开写入 `decision.md`。公开集通过不能抵消 secret 中的关键失败或待定。secret 仅有 8 条案例，单次 trial 不足以证明统计稳定性；`check-release` 的 pass 也不等于候选优于基线，仍需逐条比较和检查延迟。若根据 secret 的问题继续修改应用或 Judge，本集就转为已使用的回归集，下一次独立验证应另准备新案例。

### 4. 发布建议

保存 `decision.md`，回答：

1. 哪条原失败得到改善？哪些成功对照仍通过？没有可复现提升时为何保留基线？
2. 是否出现关键切片退化、未解释的评分冲突或额外等待？
3. 修改是否可归因？manifest 是否与实际应用一致？
4. 当前选择保留基线、保留候选、撤回候选还是继续调查？
5. 还缺哪些真实流量、成本和业务结果证据，才能进入下一发布阶段？

## 实验 7：发布观察、回退与案例更新

选择本轮目标问题，在 `decision.md` 继续填写：

| 项目     | 需要写清的内容                                                           |
| -------- | ------------------------------------------------------------------------ |
| 影子运行 | 数据来源、隔离方式、运行范围、比较指标；未来接写工具时怎样避免重复副作用 |
| 窄灰度   | 分流方式、轮椅用户等关键切片、稳定对照、最低样本与观察窗口               |
| 目标结果 | 方案是否需要人工纠正、是否仍出现与通行要求冲突的建议                     |
| 护栏     | 能力边界、关键任务失败、等待与费用预算                                   |
| 回退     | 触发条件、负责人员、恢复哪个完整版本、未结束 Run 如何处理                |
| 关闭条件 | 原问题减少、成功对照稳定、新失败已处理、业务结果已到达                   |
| 资产更新 | 新失败、重复关联、成功对照、Judge 修订和过期案例分别写入哪个版本         |

模拟开场案例的投诉延迟 18 小时。观察窗口需要覆盖业务结果出现的时间，不能用几分钟无投诉宣布修复完成。没有真实用户和线上运行时，上表是发布计划，状态写 `pending_live_evidence`，不填虚构的灰度通过率。

## 最终记录

保留以下最小交付物：

- `configs/local/lab06/` 中的基线、候选 manifest、实际 Prompt 副本、对应 DSL，以及共用 Judge。
- Dataset 名称与固定版本、两组 Experiment、运行报告与请求日志。
- 首次偏差记录、Judge 分歧记录、全部候选 trial 的人工复核。
- `release-check.json`、`decision.md`；有新增案例时再保存 v2 与原 trace 关联。

完成检查：能从一条反馈找到具体版本与运行；能解释错在工具、检索还是最终决策；能用同一组题证明修改的影响；能说明当前证据允许做什么、何时回退、何时才可关闭问题。

## 可选：模拟生产记录与安全发布判断

13 条模拟 Trace、10 条人工／Judge 对照和 4 份候选报告仍保留在 `continuous-improvement-v1`。主实验完成后，用它们补充现场不一定出现的延迟反馈、隐私、重复样本、评分器错误和未授权副作用。

```bash
uv run python scripts/validate_continuous_improvement_cases.py \
  --output reports/local/lab06/offline-dataset-check.json
```

需要评分时，将该数据集的 `predictions.template.jsonl`、`release-decisions.template.jsonl` 复制到本地，按对应 Schema 填写全部记录，再运行同一脚本的 `--curation-predictions`、`--release-decisions` 参数。报告保持 `offline_only`，不与真实 Dify Experiment 合并计算通过率。

参考：[Dify Chatflow API](https://docs.dify.ai/en/api-reference/guides/chatflow)、[Phoenix Experiment 与重复运行](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments/repetitions)、[Phoenix 重新评分](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments/using-evaluators)。
