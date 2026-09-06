# Lab 06：Tool Calling + RAG 的持续改进

先搭建最小综合 Chatflow，再围绕轮椅用户的请求完成一次改进：运行基线，关联反馈与 trace，定位首次偏差，把问题加入评测集，只修改一个变量，检查回归结果，最后给出发布与观察条件。

本 Lab 使用已有的两个只读工具和 Lab 03 知识库。课程文旅事实都是模拟数据；Dify、模型和 Phoenix 实验需要实际运行。模拟投诉、发布候选和线上观察练习单独标明来源。

## 完成标准

- 跑通 `Start → Agent → Template → Knowledge Retrieval → LLM → Answer`。
- 使用 12 条金标运行基线和候选，保存每条 trial 的回答、耗时、评分与 trace 关联信息。
- 区分工具错误、检索缺失、证据交接错误和证据齐全后的决策错误。
- 复核 Judge 与人工的分歧，只修改一项 Prompt 规则或最终回答模型。
- 保留成功对照，检查关键人群、只读能力和耗时是否退化。
- 输出发布判断、观察窗口、停止与回退条件；没有真实线上证据时保持问题未关闭。

## 准备工作

先完成 [平台环境](../agent-platform/README.md)、Lab 01 的两个工具配置，以及 Lab 03 的 `XM-Guide-Object` 知识库。无需导入 CP05/CP06，也不依赖 Lab 04、05 的工作流。

在课程根目录执行：

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare
```

预期得到 12 个 `L06-*` 案例 ID。该命令只检查本地数据与引用，不调用 Dify、模型或 Phoenix，不产生实验结果。

材料：

| 材料 | 用途 |
| --- | --- |
| [12 条金标](../../datasets/eval/tool-rag-improvement-v1/cases.jsonl) | 原始问题、切片、证据引用、工具检查与任务验收条件 |
| [Agent Prompt](prompts/agent.txt) | 查询天气和候选，保留原始约束，输出待检索信息 |
| [基线回答 Prompt](prompts/answer-v0.txt) | 根据两类信息生成建议 |
| [候选回答 Prompt](prompts/answer-v1.txt) | 在基线上增加一段约束核对规则 |
| [Judge Prompt](prompts/judge-v1.txt) | 评估最终建议，允许 `pass / fail / review` |
| [发布记录模板](release.template.json) | 固定模型、Prompt、知识库、工具及运行预算 |
| [模拟生产记录](../../datasets/eval/continuous-improvement-v1/README.md) | 延迟投诉、评分冲突、安全事件和发布判断补充练习 |

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
- 使用 Lab 01 已跑通的模型，并记录完整模型配置。
- Tool List 只启用 `weather_forecast`、`poi_search`。可从已配置的工具中选取，无需重新导入整个目录。
- Query 绑定 `sys.query`，Context 留空。
- Maximum iterations：先设为 `4`。两类查询可能分两轮执行，取得结果后还需要一轮输出摘要。
- Instruction 粘贴 [Agent Prompt](prompts/agent.txt)。

两个工具的日期字段不同：

| 工具 | 日期字段 | 本次输入为 2026 年 9 月 8 日时 |
| --- | --- | --- |
| `weather_forecast` | `start_date`、`end_date` | 都是 `2026-09-08` |
| `poi_search` | `date` | `2026-09-08` |

Agent 输出要包含原始要求、工具实际结果和待检索景点。检查“全程坐轮椅”“只到游客中心”“不接受替代”等限制是否仍在。摘要是模型输出，工具是否执行正确必须回到原始工具调用核对。

课程目录的 `poi_search` 返回候选名称和标签，不保证覆盖全部场馆；没有返回某场馆不能直接证明其不存在，也不能由“室内”或“无障碍”标签推断全程可达。

### 3. Template 节点

在 Agent 与知识检索之间增加“模板转换 / Template”节点，命名为 `整理检索查询`，配置两个输入：

| 变量名 | 绑定 |
| --- | --- |
| `query` | `sys.query` |
| `agent_text` | `Agent.text` |

粘贴 [query.jinja](prompts/query.jinja)：

```jinja
用户要求：{{ query }}
核对事项：{% if "待检索：" in agent_text %}{{ agent_text.rsplit("待检索：", 1)[-1] }}{% else %}{{ query }}{% endif %}
```

该节点没有模型调用。当前 Agent 的 text 可能混入多轮工具调用中的说明；直接把长摘要用于检索，可能未召回必要限制。模板保留原始要求，只追加最后的待检索事项；标记缺失时退回原始问题，不返回空查询。原始工具摘要仍传给最终 LLM。

在 trace 中检查模板输出是否包含指定景点、实际候选和约束。模板只能整理文本，不能证明 Agent 选对了候选；发现遗漏时仍应定位交接偏差。

### 4. Knowledge Retrieval 节点

- 知识库选择 Lab 03 的 `XM-Guide-Object`。
- Query 绑定 `整理检索查询.output`，不能改成固定的“厦门有哪些景点”。
- 采用 Lab 03 已验证的检索配置；若尚未确定，可先从语义检索、Top-K `6`、阈值 `0.50`、Reranker 关闭开始。
- 确认节点设置与知识库设置各自的作用，记录本节点实际参数。

首次冒烟可以调整绑定和检索设置；开始基线实验后固定这些条件。若之后因检索问题必须调整，单独作为检索候选重跑，不能混入回答 Prompt 或模型对照。

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

最终模型同时读取原始要求、工具摘要和检索证据，不能只读取 Agent 摘要。不要把尖括号内的说明当成字面文本保存。

Answer 只绑定 `生成建议.text`，不增加润色节点。在应用功能中开启“引用和归属 / Citation and Attribution”，便于 API 返回检索资源；不同版本的资源字段可能不完整，仍需核对节点 Context。

### 6. 冒烟测试

发布应用，输入：

```text
2026 年 9 月 8 日去厦门，同行者全程坐轮椅。想去植物园，也接受其他景点，请结合天气给出建议。
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
2026 年 9 月 8 日去厦门，我坐轮椅，只到植物园游客中心，不游览园区。查一下天气和景点资料，这样是否有无障碍设施，最晚几点进去？
```

```text
只查 2026 年 9 月 8 日厦门天气，告诉我会不会下雨，不需要景点推荐。
```

后一条不应调用景点工具。固定的知识检索节点仍会运行，因此本结构不是按需路由的最短路径；记录其耗时即可，本实验先固定结构研究改进效果。

如果工具不能连接、节点变量为空或 Phoenix 中找不到对应运行，先修复环境与绑定。业务答案不正确可以作为基线失败；不要通过改金标把它判成成功。

## 实验 2：冻结条件并运行基线

### 1. 环境变量与版本记录

在本地 `.env` 中填写：

```env
DIFY_BASE_URL=<宿主机可访问的 Dify 地址>
DIFY_LAB06_API_KEY=<Lab 06 已发布 Chatflow 的 API key>
PHOENIX_ENDPOINT=<宿主机可访问的 Phoenix 地址>
PHOENIX_API_KEY=<Phoenix API key>
JUDGE_BASE_URL=<评测模型的 OpenAI 兼容接口地址>
JUDGE_MODEL=<评测模型名>
JUDGE_API_KEY=<评测模型 API key>
```

脚本从宿主机访问服务。Dify 容器访问 Travel Core、Phoenix 时，按 [平台地址说明](../agent-platform/README.md) 配置 Docker Desktop、Compose 网络或 Linux gateway 对应地址。

在仓库根目录执行：

```bash
mkdir -p reports/local/lab06
cp labs/06-evals-release/release.template.json reports/local/lab06/baseline.manifest.json
```

编辑副本，填完所有占位值；检索参数必须改成应用实际值。`p95_budget_s=120` 是可调整的课堂预算，应在运行前确定，两版使用相同预算。

保存基线 DSL 导出到本地报告目录，并记录发布日期。manifest 是人工声明，脚本不会替你切换 Dify 模型或发布 Prompt；必须与实际已发布图逐项核对。

### 2. 创建 Phoenix Dataset

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare --apply
```

脚本把 12 条问题、验收要求和引用的冻结事实写入 `lab06-tool-rag-v1`，返回 Dataset ID 与版本 ID。记录 `dataset_version`，后面命令替换 `<版本 ID>`。

来源直接取自课程权威文档对象与天气实现，没有生成第二套 POI fixture。本地哈希校验不能证明远程 Travel Core 部署了同一修订，仍需用冒烟记录核对实际工具结果。相同名称已经有不同内容时脚本会停止，避免覆盖旧基线。

### 3. 基线实验

先检查本地参数：

```bash
uv run python scripts/run_phoenix_lab06_eval.py run \
  --dataset-version '<版本 ID>' \
  --manifest reports/local/lab06/baseline.manifest.json \
  --output reports/local/lab06/baseline.json \
  --with-llm-judge --dry-run
```

本脚本的 `--dry-run` 只做本地预检，不调用模型。去掉该参数才实际运行：

```bash
uv run python scripts/run_phoenix_lab06_eval.py run \
  --dataset-version '<版本 ID>' \
  --manifest reports/local/lab06/baseline.manifest.json \
  --output reports/local/lab06/baseline.json \
  --with-llm-judge
```

默认每题 3 次，共 36 次 Chatflow 请求，Judge 在请求完成后评分。不要同时编辑或发布应用。工具、检索与生成调用来自真实 Dify；金标事实仍是课程模拟资料。

在 Phoenix 的 Dataset → Experiments 中打开 `lab06-baseline`，查看：

| 评分或记录 | 能检查什么 | 还需要什么 |
| --- | --- | --- |
| `response_available` | API 是否成功返回非空回答 | 非空不等于任务完成 |
| `evidence_coverage` | API 资源是否包含所需对象 | 资源缺失或不完整时核对检索节点 |
| `citation_sources` | 答案对象 ID 是否来自 API 资源 | 原文是否支持结论仍需语义判断 |
| `task_constraints_<版本>` | 最终建议是否满足金标约束 | Judge 分歧需人工复核 |
| `elapsed_s` | 整次 Chatflow HTTP 请求耗时 | 不包含后续 Judge 和 trace 查询，不是 TTFT |
| `api_reported_usage` | Dify API 实际返回的用量 | 缺失不按零处理；未验证聚合口径前不称为全链成本 |
| `dify_trace_id` | 用新会话 ID 关联到的原始 trace | 未匹配时按 conversation ID 人工定位 |

`review` 表示需要复核，不能改写成 pass。超时和错误保留在请求日志与失败分母里，不能只统计成功响应。

若运行中断，先查看 `baseline.requests.jsonl`、Phoenix Experiment 和本地报告状态。请求可能已经在 Dify 完成，不要直接覆盖报告重跑；核对后用新运行名称和报告文件记录重跑范围。

## 实验 3：从反馈定位偏差并回流案例

### 1. 关联同一次运行

选择 L06-001 或其同义表达 L06-002，记录：

| 字段 | 记录 |
| --- | --- |
| 应用版本、manifest、Experiment ID | |
| case ID、trial、message ID、conversation ID、原始 trace ID | |
| 用户原始要求 | |
| 工具参数与结果 | |
| 检索原文与最终 LLM 的输入 | |
| 最终建议、Judge 判断 | |
| 人工依据、首次偏差或未知项 | |

再阅读模拟记录中的 `trace-prod-001`：即时点赞与 18 小时后的投诉属于不同时间、不同维度的证据。该记录不是你刚刚产生的真实 trace，不能把其投诉写到个人运行名下。

对自己的输出做人工场景核验：如果用户按建议游览，是否仍能满足其“全程坐轮椅”的要求？这属于离线验收，不是实际游客使用后的业务 outcome。

### 2. 分诊与修改对象

| 观察到的证据 | 优先检查 | 回流位置 |
| --- | --- | --- |
| 天气或景点日期错误、调用了不需要的工具 | Agent 指令、参数和工具调用 | Tool 专项，并保留端到端 case |
| 原话有约束，Agent 摘要或检索 Query 丢失了约束 | 节点交接 | Context／端到端 |
| 检索未取得设施限制，或读了错误文档 | Query、知识库与检索配置 | RAG 专项，并保留端到端 case |
| 原始证据齐全并已进入最终模型，仍承诺全园可达 | 最终决策 Prompt 或模型 | Decision／端到端 |
| 允许只到游客中心，Judge 却按全园请求判错 | 评分标准与人工解释 | Judge 校准 |
| 只读应用声称已经预订成功 | 能力边界与答案真实性 | 安全／端到端 |

首次偏差是排查起点。缺少节点输入时写“待核对”；不要只凭最终答案断定检索失败。若看到实际越权写入或隐私问题，先停止相关能力并保留证据，再分析修复。

### 3. 新案例与成功对照

复制案例文件到 `reports/local/lab06/cases-v2.jsonl`，准备后续回流版本：

```bash
cp datasets/eval/tool-rag-improvement-v1/cases.jsonl reports/local/lab06/cases-v2.jsonl
```

从自己的真实运行中选择尚未被覆盖的失败或边界表达，人工核对参考要求，追加唯一 case ID、原始问题、`source_trace_id`、切片与证据对象。先去掉个人信息。若只是 L06-001 的重复表现，记录关联与频次，不重复加权。

保留 L06-004、005、008、010 等成功对照。它们用于检查是否把“谨慎处理轮椅限制”修成了“拒绝所有出行建议”。原 trace 的模型回答不能直接作为金标。

本轮单变量比较继续使用已冻结的 v1。新问题形成下一轮：

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare \
  --cases reports/local/lab06/cases-v2.jsonl \
  --dataset-name lab06-tool-rag-v2 --apply
```

下一轮基线和候选都要使用新 Dataset 的名称与版本重跑。不能拿 v1 基线与 v2 候选直接比较。

## 实验 4：复核 Judge

在 12 条基线样本中，每条先选择第 1 次 trial，独立标注人工 `pass / fail / review`、理由和证据，再看 Judge 的评分。对轮椅、缺少日期、游客中心、只读能力等分歧，继续查看其余 trial。

记录：

| case / trial | 人工 | Judge | 证据与分歧原因 | 采用的结论 |
| --- | --- | --- | --- | --- |
| | | | | |

计算已能明确判断的样本一致率，同时单列待定数。误放行指人工 fail、Judge pass；误拒绝指人工 pass、Judge fail。没有正例或负例时，不能声称已经验证该方向的准确性。

需要修订时，复制 `judge-v1.txt` 到本地 `judge-v2.txt`，只修改错误的判定规则，保留输入字段。对已保存的回答重新评分：

```bash
uv run python scripts/run_phoenix_lab06_eval.py rejudge \
  --report reports/local/lab06/baseline.json \
  --judge-prompt reports/local/lab06/judge-v2.txt \
  --output reports/local/lab06/baseline.rejudged.json
```

该命令只调用 Judge，不重新执行 Dify。新评分器名称包含 Prompt、服务入口和模型的哈希，旧评分保留。用新版 Judge 比较候选时，两版都必须采用同一评分器。

有限样本上的修订只说明当前分歧得到处理。仍需保留未用于改 rubric 的样本验证；未经校准的 Judge 不单独决定安全放行。

## 实验 5：单变量修改与回归

### 1. 写出假设

只有确认必要证据已经传入最终模型，才采用以下假设：

```text
目标：减少轮椅用户请求中“局部设施可达被扩大成全程可达”的错误。
变量：最终 LLM 的 answer Prompt，或该节点的模型，二选一。
固定：Agent 模型与指令、查询模板、工具、知识库、检索参数、Memory、Dataset 版本、Judge、trial 数、预算。
失败条件：原问题没有改善；成功对照退化；出现事实或能力边界错误；耗时超过预定预算。
```

如果偏差在工具或检索层，记录正确的修复方向，先完成该层修复并重新建立基线。不能把改错层的结果当成最终回答模型的能力比较。

### 2. 选择候选

二选一：

- **Prompt 候选**：把最终节点 System Prompt 的基础文本从 `answer-v0.txt` 换为 `answer-v1.txt`，原有 Context 绑定保留。两文件只差一段约束核对规则。
- **模型候选**：最终节点 Prompt 不变，只更换最终回答模型；Agent 节点仍使用原模型。记录实际服务商、模型名、可获得的版本和兼容参数。如果换模型必须改其他配置，标记为组合变更，不能声称只换了模型。

基线已经全部通过时，不保证候选能更好。可以比较重复运行的稳定性和等待代价，结果没有改善就保留基线；不要故意破坏基线来制造提升。需要固定失败演示时，用明确标记的模拟记录讨论。

复制并填写候选 manifest：

```bash
cp reports/local/lab06/baseline.manifest.json reports/local/lab06/candidate.manifest.json
```

把 `release_id` 改为 `lab06-prompt-v1` 或 `lab06-model-b`，仅改变对应的 `answer_prompt_revision` 或 `answer_model`。发布候选并保存本地 DSL，核对实际配置与声明一致。

### 3. 同集复跑

```bash
uv run python scripts/run_phoenix_lab06_eval.py run \
  --dataset-version '<与基线相同的版本 ID>' \
  --manifest reports/local/lab06/candidate.manifest.json \
  --output reports/local/lab06/candidate.json \
  --with-llm-judge
```

若上一节修订了 Judge，增加 `--judge-prompt reports/local/lab06/judge-v2.txt`，比较时使用 `baseline.rejudged.json`。

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

| 退出码 | 状态 | 含义 |
| --- | --- | --- |
| `0` | `pass` | 课堂只读候选通过当前固定集与人工复核，进入发布判断 |
| `1` | `block` | 任务、工具、关键约束或耗时预算失败 |
| `2` | `review` | 条件不可比、trial 不完整、证据缺失或人工复核未完成 |

检查会核对 Dataset 版本、事实来源哈希、评分器、trial 数和单变量范围。复核必须与全部候选 run ID 一一对应。失败与待定都不能用更高平均分抵消。

这条本地命令可作为 CI 的评测检查步骤，但没有替你配置远程 CI。依据课件正文填写触发规则：Prompt、回答模型、工具 Schema、RAG 数据、评分器变更分别触发哪些评测；定时检查还要覆盖未提交代码但外部依赖变化的情况。

课程应用只有查询能力。此处 pass 不证明预订、支付、幂等或跨租户安全已验证；这些问题继续使用已有专项证据。无需为本 Lab 新增写工具。

### 3. 发布建议

保存 `decision.md`，回答：

1. 哪条原失败得到改善？哪些成功对照仍通过？没有可复现提升时为何保留基线？
2. 是否出现关键切片退化、未解释的评分冲突或额外等待？
3. 修改是否可归因？manifest 是否与实际应用一致？
4. 当前选择保留基线、保留候选、撤回候选还是继续调查？
5. 还缺哪些真实流量、成本和业务结果证据，才能进入下一发布阶段？

## 实验 7：发布观察、回退与案例更新

选择本轮目标问题，在 `decision.md` 继续填写：

| 项目 | 需要写清的内容 |
| --- | --- |
| 影子运行 | 数据来源、隔离方式、运行范围、比较指标；未来接写工具时怎样避免重复副作用 |
| 窄灰度 | 分流方式、轮椅用户等关键切片、稳定对照、最低样本与观察窗口 |
| 目标结果 | 方案是否需要人工纠正、是否仍出现与通行要求冲突的建议 |
| 护栏 | 能力边界、关键任务失败、等待与费用预算 |
| 回退 | 触发条件、负责人员、恢复哪个完整版本、未结束 Run 如何处理 |
| 关闭条件 | 原问题减少、成功对照稳定、新失败已处理、业务结果已到达 |
| 资产更新 | 新失败、重复关联、成功对照、Judge 修订和过期案例分别写入哪个版本 |

模拟开场案例的投诉延迟 18 小时。观察窗口需要覆盖业务结果出现的时间，不能用几分钟无投诉宣布修复完成。没有真实用户和线上运行时，上表是发布计划，状态写 `pending_live_evidence`，不填虚构的灰度通过率。

## 最终记录

保留以下最小交付物：

- 基线、候选 manifest 与对应的本地 DSL 导出。
- Dataset 名称与固定版本、两组 Experiment、运行报告与请求日志。
- 首次偏差记录、Judge 分歧记录、全部候选 trial 的人工复核。
- `release-check.json`、`decision.md`；有新增案例时再保存 v2 与原 trace 关联。

完成检查：能从一条反馈找到具体版本与运行；能解释错在工具、检索还是最终决策；能用同一组题证明修改的影响；能说明当前证据允许做什么、何时回退、何时才可关闭问题。

## 可选：模拟生产记录与安全发布判断

13 条模拟 Trace、10 条人工／Judge 对照和 4 份候选报告仍保留在 `continuous-improvement-v1`。主实验完成后，用它们补充现场不一定出现的延迟反馈、隐私、重复样本、评分器错误和未授权副作用。

```bash
uv run python scripts/archived/validate_continuous_improvement_cases.py \
  --output reports/local/lab06/offline-dataset-check.json
```

需要评分时，将该数据集的 `predictions.template.jsonl`、`release-decisions.template.jsonl` 复制到本地，按对应 Schema 填写全部记录，再运行同一脚本的 `--curation-predictions`、`--release-decisions` 参数。报告保持 `offline_only`，不与真实 Dify Experiment 合并计算通过率。

参考：[Dify Chatflow API](https://docs.dify.ai/en/api-reference/guides/chatflow)、[Phoenix Experiment 与重复运行](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments/repetitions)、[Phoenix 重新评分](https://arize.com/docs/phoenix/datasets-and-experiments/how-to-experiments/using-evaluators)。
