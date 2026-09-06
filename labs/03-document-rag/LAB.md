# Lab 03：文档 RAG、检索调优与 Query Rewrite

本 Lab 新建一条最小 Chatflow，不复制 Lab 02 的 Tool Calling 节点。先用 `Start → Knowledge Retrieval → LLM → Answer` 建立基线，再加入 Query Rewrite，对同一批问题做修改前后对照。

主实验只处理单次 RAG。依赖型多跳放在文末扩展实验，不进入必做主流程。

## 完成标准

完成本 Lab 后，你应能提交：

- 两个使用同一数据修订和 embedding 的 Dify 知识库；
- 一条可以回答、引用和拒答的最小 RAG Chatflow；
- D0–D4 五组真实 Dify Knowledge API 检索报告；
- 从无 Rewrite trace 建立的 Phoenix Dataset；
- 使用同一 Dataset 跑出的 baseline 与 rewrite 两组 Phoenix Experiment；
- 10 条 Query Rewrite 样本的修改前后记录；
- Rewrite 约束、检索结果和最终答案三层评测；
- 每次修改的 `keep / revert` 判断，以及对应 trace 或报告。

## 主工作流

### 第一版：无 Rewrite 基线

```text
Start
  → Knowledge Retrieval
  → LLM
  → Answer
```

### 第二版：加入 Rewrite

```text
Start
  → LLM（Query Rewrite）
  → Knowledge Retrieval
  → LLM
  → Answer
```

Rewrite 节点只生成检索查询，不回答用户问题。最终回答节点同时读取用户原始问题、改写后的查询和检索证据，回答对象仍是用户原始问题。

## 材料

以下目录都相对课程根目录：

- 源 PDF：`datasets/rag/document-ai/v1/source/xiamen_travel_service_training_v1.pdf`
- 解析依据：`datasets/rag/document-ai/v1/source/source_spec.json`
- 整页文本：`datasets/rag/document-ai/v1/parsed/text-only.jsonl`
- 对象级语料：`datasets/rag/document-ai/v1/parsed/object-aware.jsonl`
- Dify 导入包：`datasets/rag/document-ai/v1/dify-import/`
- RAG 金标：`datasets/eval/rag-v1/cases.jsonl`
- 检索评测：`scripts/run_dify_native_rag_eval.py`
- RAG 评分器：`src/training_eval/rag.py`
- Phoenix Experiment runner：`scripts/run_phoenix_lab03_eval.py`

## 时间安排

| 实验                            |    时间 | 产物                                  |
| ------------------------------- | ------: | ------------------------------------- |
| 0. 材料与环境预检               | 10 分钟 | 预检结果                              |
| 1. 建立两个知识库               | 10 分钟 | `XM-Guide-Text`、`XM-Guide-Object`    |
| 2. 搭建最小 RAG Chatflow        | 10 分钟 | 无 Rewrite 基线                       |
| 3. 证据层与语料形态对照         | 10 分钟 | Page/Object/Fine 记录                 |
| 4. D0–D4 检索调优               | 20 分钟 | 五组报告与保留判断                    |
| 5. 从 trace 建 Dataset 并跑基线 | 15 分钟 | Dataset、baseline Experiment          |
| 6. Query Rewrite A/B            | 20 分钟 | rewrite Experiment、10 条修改前后记录 |
| 7. 结果整理与回归               | 15 分钟 | 最终配置、trace 和未通过项            |

## 实验 0：材料与环境预检

### 目标

确认冻结语料、12 道 RAG 金标和评分器可以运行。预检不调用 Dify，不能证明知识库检索已经通过。

### 操作

在仓库根目录执行：

```bash
uv run python scripts/export_rag_for_dify.py
```

它负责把（解析后的）PDF 内容整理为导入 RAG 的形式；分别模拟了仅文本（结果在`datasets/rag/document-ai/v1/dify-import/text-only`）和对象识别（`datasets/rag/document-ai/v1/dify-import/object-aware`）。

```bash
uv run python scripts/validate_rag_cases.py
```

它检查是否各案例的格式、预期结果等，此时应该会显示验证 12 条案例状态正常。

然后执行

```bash
uv run pytest \
 tests/training_eval/test_document_rag_eval.py \
 tests/scripts/test_run_dify_rag_eval.py \
 tests/scripts/test_run_phoenix_lab03_eval.py \
 tests/travel_core/test_rag.py
```

测试应该都能通过。

## 实验 1：建立两个知识库

### 目标

建立整页文本基线和对象级 Parent-child 知识库。两组配置除语料形态和切分方式外保持一致。

### 0. 安装嵌入模型

提前配置好 embedding 模型，可以使用硅基流动的免费嵌入模型（`BAAI/bge-m3`)。如果配置了智谱，也可以使用它提供的 embedding 模型。

### 1. 创建整页文本知识库

在 Dify 中创建知识库：

```text
名称：XM-Guide-Text
索引方式：High Quality
Embedding：BAAI/bge-m3
文档模式：General
检索设置：Vector Search
Top K：6
Score Threshold：0.5
```

导入目录中的 7 个 Markdown 文件：

```text
datasets/rag/document-ai/v1/dify-import/text-only/
```

General 切分尽量让每个文件保持为一个页面块。导入后检查文档数为 7。

### 2. 创建对象级知识库

在 Dify 界面创建知识库：

```text
名称：XM-Guide-Object
文档模式：Parent-child
父块：全文（Full Doc）
子块：换行符、最大 256 字符
索引方式：High Quality
Embedding：与 XM-Guide-Text 使用同一提供商、同一模型
检索设置：Vector Search
Top K：6
Score Threshold：0.5
Rerank：关闭（文本库也保持关闭）
```

导入目录中的 14 个 Markdown 文件：

```text
datasets/rag/document-ai/v1/dify-import/object-aware/
```

父块选择全文（Full Doc），让每个对象文件作为完整父块返回；等待索引完成，确认文档数为 14。若单批上传限制为 5 个，可在同一知识库分三批添加：5 + 5 + 4；文本库分为 5 + 2。

> 批量写入原生字段与过滤操作见文末“备选实验：脚本导入与 metadata 过滤”。

### 3. 配置本地环境变量

获取知识库的 API key（两个知识库共用一个 key），获取方式如下：
![知识库 API key](../../images/dify-knowledge-api.png)

在课程根目录 `.env` 中新增：

```env
DIFY_KB_API_KEY=<Dify Knowledge API key>
```

### 4. 固定比较条件

填写：

| 配置          | `XM-Guide-Text` | `XM-Guide-Object` |
| ------------- | --------------- | ----------------- |
| 数据 revision | 1               | 1                 |
| 文档数        | 7               | 14                |
| 文档模式      | General         | Parent-child      |
| Embedding     |                 |                   |
| 索引方式      |                 |                   |
| metadata 过滤 | 关闭            | 关闭              |

### 通过条件

- 文本库正好 7 个文件；
- 对象库正好 14 个文件；
- embedding 和索引方式一致；
- 对象库使用 Parent-child；
- 文档可以回指 page、object ID 和数据 revision。

## 实验 2：搭建最小 RAG Chatflow

### 目标

先跑通单次检索、证据回答和无答案拒答。

### 1. 创建 Chatflow

在 Dify 新建 Chatflow：

```text
名称：Lab 03
Phoenix project：Lab 03
```

结构为：Start → 知识检索 → LLM → Answer

关闭对话 Memory。课堂评测时每道题使用新会话，防止前一题的内容进入下一题。

### 2. Start 节点

不动。

### 3. 知识检索节点

配置：

```text
节点名称：检索文旅资料
知识库：XM-Guide-Text
Query：Start.query
检索方式：语义（Semantic Search）
Top-K：6
Score Threshold：0.50
Reranker：关闭
```

知识库里的检索设定，和`知识检索`节点里的检索设定是独立的。这里的纯语义检索，对应语义权重为 1（或关键字检索权重是 0）。

### 4. LLM 节点

节点名称：`根据资料回答`。

将 Knowledge Retrieval 的结果绑定到 LLM Context。用户 Prompt 传入原始问题。

System Prompt：

```text
你是厦门文旅资料问答助手。

只根据上下文回答用户原始问题。
上下文没有足够证据时，明确回答“现有资料不足，无法确定”。
不要使用模型记忆补充资料中没有的事实。
遇到多个版本时，说明各版本的状态和时间，再判断当前应采用哪版。
答案末尾列出实际使用的文档名称或对象 ID；不能引用上下文中没有出现的来源。

上下文：{{上下文}}
```

这里的`{{上下文}}`通过变量选择器来选择「上下文」。

User Prompt：

```text
用户问题：{{query}}
```

Memory 关闭。记录完整模型名称。

### 5. Answer 节点

直接输出 `根据资料回答.text`，不增加第二次润色或总结。

第一版结构应为：

```text
Start → 检索文旅资料 → 根据资料回答 → Answer
```

### 6. 冒烟测试

分别使用新会话运行：

```text
周末轮渡末班几点？开航前多久停止检票？

出现大风橙色预警且已有票务占位时，处置链是什么？

厦门水族馆每天几点进行企鹅喂食？
```

在 Dify 和 Phoenix 中记录：

- 原始 query；
- 检索返回的文档、对象或 segment；
- 每条结果的 score；
- LLM 实际 Context；
- 最终回答和引用；
- 无答案题是否拒答；
- Dify run ID 与 Phoenix trace ID。

## 实验 3：页面命中为什么仍可能答错

### 目标

区分 Page、Object 和 Fine 三层证据，比较整页文本与对象级语料。

> 对象级语料是预先提取好的。实际提取效果要看源文档和选用的提取方案。课件中对此有更详细讨论。

### 1. 检查原始证据

保持实验 2 的 Chatflow 配置，分别在新会话中输入以下问题。问题取自 `datasets/eval/rag-v1/cases.jsonl`：

RAG-002：

```text
周末坐轮渡，最后一班几点开？开船前多久就不让检票了？
```

RAG-004：

```text
已经买了船票了，又遇到大风橙色预警，接下来要怎么办呢？
```

本实验临时将“已买船票”与资料中的“已有票务占位”视为同一状态，按已有占位分支评测，不考查两者在真实票务系统中的区别。此约定不意味着资料提供了具体退票或改签规则。

RAG-005：

```text
买票时显示提交超时了，怎么确认到底成功没有？
```

RAG-007：

```text
现在到开航还有 40 分钟，想退票。收多少手续费？
```

RAG-009：

```text
厦门水族馆的企鹅每天几点喂食？
```

### 2. 记录整页文本基线

对照原始资料，检查 LLM 实际收到的 Context。Text-only 和对象库都使用以下检查项；不以是否返回 object/Fine ID 判定证据是否命中。无答案题的事实与关系两列填写“不适用”，另检查召回片段是否无关、回答是否拒答。

| Case    | 命中页面/片段                               | 必要事实是否齐全                   | 条件与关系是否保留                                   | 回答结果                                            | 首次偏差                               |
| ------- | ------------------------------------------- | ---------------------------------- | ---------------------------------------------------- | --------------------------------------------------- | -------------------------------------- |
| RAG-002 | P2 标题第 1、正文第 2；另有 P7、P6 退票标题 | 是：末班 19:30、开航前 20 分钟停检 | 本题可辨认周末行与两项时间的对应关系；无显式表格结构 | 正确：19:30 开船、提前 20 分钟停检；推算 19:10 正确 | 暂无事实或关系偏差；另召回无关退票标题 |
| RAG-004 |                                             |                                    |                                                      |                                                     |                                        |
| RAG-005 |                                             |                                    |                                                      |                                                     |                                        |
| RAG-007 |                                             |                                    |                                                      |                                                     |                                        |
| RAG-009 |                                             |                                    |                                                      |                                                     |                                        |

### 3. 只替换知识库

把 Knowledge Retrieval 节点绑定从 `XM-Guide-Text` 改为 `XM-Guide-Object`，其它设置保持：

```text
Semantic Search
Top-K：6
Score Threshold：0.50
Reranker：关闭
```

重新发布，使用相同五题和新会话运行。保留两组 run ID。

填写对象库对照表，分别判断证据是否更完整、回答是否改善。允许“证据更完整但回答仍有错误”，也允许“两种方式都答对”。RAG-009 的关系完整性填“不适用”，比较是否正确拒答。

| Case    | 对象库命中证据                                                          | 条件与关系是否完整进入 LLM                                        | 回答相比 Text-only 的变化                                                    | 仍存在的问题                             | 对象库 Trace ID                    |
| ------- | ----------------------------------------------------------------------- | ----------------------------------------------------------------- | ---------------------------------------------------------------------------- | ---------------------------------------- | ---------------------------------- |
| RAG-002 |                                                                         |                                                                   |                                                                              |                                          |                                    |
| RAG-004 | P2 轮渡表第 1（0.7314）；P3 说明第 2（0.6741）；P3 流程图第 3（0.6250） | 是：完整流程对象包含橙色/红色、已有占位和核验完成等条件及对应连线 | 两次均包含停止行程、取消占位、核验、改排室内；本次明确“核验完成后”再改排行程 | “核验退票或改签状态”仍是资料未说明的补充 | `30e6e69403e3902c286724299e8b08a4` |
| RAG-005 |                                                                         |                                                                   |                                                                              |                                          |                                    |
| RAG-007 |                                                                         |                                                                   |                                                                              |                                          |                                    |
| RAG-009 |                                                                         |                                                                   |                                                                              |                                          |                                    |

## 实验 4：D0–D4 检索调优

### 目标

使用全部 12 道金标，依次比较对象化、混合检索、reranker 和更小 Top-K。每一级只改变一项。

| 级别 | 语料         | 检索     | Reranker | Top-K | 唯一主要变化        |
| ---- | ------------ | -------- | -------- | ----: | ------------------- |
| D0   | text-only    | semantic | 关闭     |     6 | 整页文本基线        |
| D1   | object-aware | semantic | 关闭     |     6 | 只换语料与切分形态  |
| D2   | object-aware | hybrid   | 关闭     |     6 | 只改检索方式        |
| D3   | object-aware | hybrid   | 开启     |     6 | 只加入真实 reranker |
| D4   | object-aware | hybrid   | 开启     |     4 | 只缩小最终 Top-K    |

前面已经完成了 D0 和 D1。「对象感知」的索引，可以让智能体得到更准确（在本例中也更快）的结果。继续在 D1 的基础上尝试提升。

### 0. 准备

获取应用的 API key 并增加到 `.env` 中：

```
DIFY_LAB03_API_KEY=<Lab 03 Chatflow API key>
```

### 1. 按 D0–D2 配置单独测知识库

以下测试用到了 Dify 的知识库 API 对知识库本身做测试，可以快速判断知识库是否能够有效召回（效果等同于在知识库界面上手工进行测试）。结果不会进入 Phoenix。

```bash
uv run python -m scripts.run_dify_native_rag_eval \
  --preset text-semantic-k6 \
  --run-name "lab03-d0-text-semantic-k6"

uv run python -m scripts.run_dify_native_rag_eval \
  --preset object-semantic-k6 \
  --run-name "lab03-d1-object-semantic-k6"

uv run python -m scripts.run_dify_native_rag_eval \
  --preset object-hybrid-k6 \
  --run-name "lab03-d2-object-hybrid-k6"
```

比较一下，D2 和 D1 是否有区别？

### 2. 按 D3–D4 配置单独测知识库

先在 `XM-Guide-Object` 配置 reranker。模型为：

```text
BAAI/bge-reranker-v2-m3
```

或记录你实际的 provider 和模型。

运行：

```bash
uv run python -m scripts.run_dify_native_rag_eval \
  --preset object-hybrid-rerank-k6 \
  --run-name "lab03-d3-object-hybrid-rerank-k6"

uv run python -m scripts.run_dify_native_rag_eval \
  --preset object-hybrid-rerank-k4 \
  --run-name "lab03-d4-object-hybrid-rerank-k4"
```

### 3. 比较结果

```bash
for file in reports/local/dify-native-rag/lab03-d[0-4]-*.report.json
do
  jq -r '[.run_name,
    .config.search_method,
    .config.reranking_enable,
    .config.top_k,
    .score.retrieval.object_recall_at_k,
    .score.retrieval.complete_evidence_rate,
    .score.retrieval.ndcg_at_k,
    .score.retrieval.unanswerable_accuracy,
    .live_retrieval.p95_ms_per_case] | @tsv' "$file"
done
```

填写：

| 级别 | 对象召回 | 完整证据率 | nDCG   | 无答案准确率 | 检索延迟 p95 | 候选/撤销 | 与前一配置相比退化的 Case |
| ---- | -------- | ---------- | ------ | ------------ | ------------ | --------- | ------------------------- |
| D0   |          |            |        |              |              | baseline  |                           |
| D1   |          |            |        |              |              |           |                           |
| D2   |          |            |        |              |              |           |                           |
| D3   | 88.24%   | 81.82%     | 0.8079 | 100%         | 955.81 ms    | 候选      | 无新增；RAG-006 仍缺证据  |
| D4   | 82.35%   | 72.73%     | 0.7624 | 100%         | 716.53 ms    | 撤销      | RAG-004：漏掉流程图       |

对象召回率（Object Recall）：各问题命中的金标对象数量之和，除以各问题所需金标对象数量之和；不是逐题 Recall 的简单平均。
完整证据率：有多少问题找齐了全部必要证据？
nDCG（归一化折损累积增益）：相关资料是否排在前面？两组配置都找到了全部资料，但 A 组的相关资料都在最前面，而 B 都排在后面，A的 nDCG 更高。
无答案准确率：比如，案例 009 应该回答没有答案。
p95 检索延迟：每道题检索总耗时从少到多排在一起的 95% 分位。

### 判断顺序

1. 先检查对象召回、完整证据率和 RAG-009（无答案）--相关资料都召回；
2. 质量没有退化后，再比较 nDCG--优化排序；
3. 前两项通过后，再比较检索 p95--优化延迟；
4. 某一级发生必要证据缺失或错误自信回答时，该级撤回；

## 实验 5：从 trace 建 Dataset 并跑基线

### 目标

把真实 Chatflow 运行转成可重复执行的 Phoenix Dataset。Dataset 只从 trace 取得用户原始问题；注意答案部分还是需要根据参考答案人工来改写。

本实验固定 Chatflow 配置：

```text
知识库：XM-Guide-Object
检索方式：Hybrid Search
Top-K：6
Score Threshold：0.50
Reranker：关闭
Rewrite：关闭
```

### 1. 产生 10 条基线 trace

使用下列输入逐条运行，每题开启新会话：

| ID     | 用户原始问题                                               | 必须保留的含义                             | 对应金标 |
| ------ | ---------------------------------------------------------- | ------------------------------------------ | -------- |
| RW-001 | 厦门市博物馆礼拜一开不开，最晚几点能进？                   | 厦门市博物馆、周一、开放状态、停止入馆时间 | RAG-001  |
| RW-002 | 周末坐轮渡，最晚一班几点，发船前多久就不让检票了？         | 周末、末班、停止检票提前量                 | RAG-002  |
| RW-003 | 已经买了船票了，又遇到大风橙色预警，接下来要怎么办呢？     | 已买船票、橙色预警、完整处置顺序           | RAG-004  |
| RW-004 | 买票时显示提交超时了，怎么确认到底成功没有？               | 提交超时、查询最终状态、确认是否成功       | RAG-005  |
| RW-005 | 现在离开船还有 40 分钟，退票要扣多少手续费？               | 当前版本、40 分钟、手续费比例              | RAG-007  |
| RW-006 | 资料里一会儿写 30 分钟，一会儿写 60 分钟，退票到底按哪版？ | 30/60 分钟、版本冲突、当前适用版本         | RAG-008  |
| RW-007 | 厦门水族馆的企鹅每天啥时候喂？                             | 厦门水族馆、企鹅、每天、时间；不得补写答案 | RAG-009  |
| RW-008 | 厦门园林植物园是不是每条路线都能坐轮椅走？                 | 厦门园林植物园、全部路线、轮椅适用性       | RAG-010  |
| RW-009 | 室内无障碍馆里，周一开的，哪个最晚停止入馆？               | 室内、无障碍、周一开放、最晚停止入馆       | RAG-011  |
| RW-010 | 周末坐末班轮渡的话，轮椅旅客最迟几点到码头？               | 周末、末班、轮椅旅客、最迟到达时间         | RAG-012  |

每条 trace 至少检查：

- 顶层输入是用户原始问题；
- Knowledge Retrieval span 中有 Top-K 文档名、score 和 content；
- 最终输出是 Answer 节点文本；
- Dify run 与 Phoenix trace 可以按时间和输入对应。

### 2. 从 trace 创建 Phoenix Dataset

1. 在 Phoenix 中打开项目 `Lab 03`。
2. 从 RW-001 的顶层 trace 选择若干条，并创建 dataset。
3. Dataset 名称填写：

   ```text
   lab03-rag
   ```

4. Input 只保留用户原始问题，推荐格式：

   ```json
   {
     "query": "厦门市博物馆礼拜一开不开，最晚几点能进？"
   }
   ```

5. Output 不保留模型旧答案，改成该题的金标约束：

   ```json
   {
     "case_id": "RW-001",
     "answerable": true,
     "must_contain": ["周一闭馆", "16:30"],
     "must_not_contain": ["周一开放"],
     "gold_object_ids": ["XM-GUIDE-001-P1-VENUE-TABLE"]
   }
   ```

6. 从其余若干条 trace 继续向同一 Dataset 添加样本。
7. 每条样本的 `must_contain`、`must_not_contain` 和 `gold_object_ids` 从 `datasets/eval/rag-v1/cases.jsonl` 中对应 case 复制；`case_id` 使用 RW 编号。
8. RW-007 的 `answerable` 为 `false`，`gold_object_ids` 为空数组；它仍需保留 RAG-009 的 `must_contain` 与 `must_not_contain`。
9. 保存后确认 Dataset 不含 system prompt 或模型思考文本。

另外两个 case 的输入和输出，可以参考，跑第一批 3 个案例；其余可以按此填写：

#### RW-002

输入：

```json
{
  "query": "周末坐轮渡，最晚一班几点，发船前多久就不让检票了？"
}
```

输出：

```json
{
  "case_id": "RW-002",
  "answerable": true,
  "must_contain": ["19:30", "20"],
  "must_not_contain": [],
  "gold_object_ids": ["XM-GUIDE-001-P2-FERRY-TABLE"]
}
```

#### RW-003

输入：

```json
{
  "query": "已经买了船票了，又遇到大风橙色预警，接下来要怎么办呢？"
}
```

输出：

```json
{
  "case_id": "RW-003",
  "answerable": true,
  "must_contain": ["停止轮渡", "取消占位", "核验最终状态", "改排室内行程"],
  "must_not_contain": ["维持计划"],
  "gold_object_ids": ["XM-GUIDE-001-P3-WIND-FLOW"],
  "acceptance_notes": "本题接受已买船票与已有占位等价。应先停止轮渡与岛上行程，再取消占位并核验最终状态，核验完成后改排室内行程。不得补充资料未说明的退票、改签规则或办理渠道。"
}
```

### 3. 运行 baseline Experiment

`.env` 共用 Lab-01 / Lab-02 配置，只需要增加：

```env
DIFY_LAB03_API_KEY=<Chatflow API key>
```

先运行 dry-run：

```bash
uv run python scripts/run_phoenix_lab03_eval.py \
  --experiment-name "lab03-baseline" \
  --with-llm-judge \
  --dry-run
```

运行完整实验：

```bash
uv run python scripts/run_phoenix_lab03_eval.py \
  --experiment-name "lab03-baseline" \
  --with-llm-judge
```

Runner 会为每条 task run 保存 Dify 回答和 `retriever_resources`，并运行三个 Evaluator：

| Evaluator             | 类型      | 检查内容                                               |
| --------------------- | --------- | ------------------------------------------------------ |
| `answer_requirements` | code      | `must_contain` 全部出现，`must_not_contain` 全部不出现 |
| `evidence_complete`   | code      | 可回答题的全部`gold_object_ids` 是否进入 Top-K         |
| `grounded_answer`     | LLM judge | 回答是否满足问题，并且业务事实能由实际检索证据支持     |

打开 Phoenix Experiment，逐条查看 task output、score、label 和 explanation。先看 `evidence_complete`，再看 `grounded_answer`；检索证据缺失时，不先修改回答 Prompt。

### 通过条件

- Dataset 正好 10 条，Input 是原始问题，Output 是人工核对后的金标约束；
- baseline Experiment 正好产生 10 条 task run；
- 每条 task run 都能看到 `retrieved_resources`；
- 三个 Evaluator 均有结果；
- 记录首个检索失败样本和首个生成失败样本，不要求 baseline 全部通过。

## （备选）实验 6：Query Rewrite A/B

### 目标

在固定知识库与检索参数下，只增加 Query Rewrite 节点，检查改写是否保留原意，以及检索证据是否改善。

本实验固定使用：

```text
知识库：XM-Guide-Object
检索方式：Hybrid Search
Top-K：6
Score Threshold：0.50
Reranker：关闭
回答模型与 Prompt：保持不变
```

即使 D0–D4 得出了其它最终配置，Rewrite A/B 仍使用上面的固定配置，避免同时改变两个变量。

### 1. 增加 Rewrite LLM

在 Start 和 Knowledge Retrieval 之间加入 LLM 节点，命名为 `改写检索查询`。

开启结构化输出，使用以下 JSON Schema：

```json
{
  "type": "object",
  "properties": {
    "rewritten_query": {
      "type": "string"
    }
  },
  "required": ["rewritten_query"],
  "additionalProperties": false
}
```

System Prompt：

```text
你只负责把用户原始问题改写成适合知识库检索的独立查询，不回答问题。

规则：
1. 保留原问题中的实体、地点、日期、时间、数字、版本、范围、比较关系和否定含义。
2. 可以补全口语表达和省略的检索词，但不能增加用户没有提供的业务事实。
3. 不得把“全部、最晚、当前、周末、已有”等限定条件删除或弱化。
4. 不得根据模型记忆写入答案、结论、场馆名单、时间或比例。
5. 无答案问题也只改写查询，不能猜测答案。
6. 只输出符合 Schema 的 JSON。
```

User Prompt：

```text
用户原始问题：{{query}}
```

模型 temperature 设为 `0` 或课堂模型支持的最低值。关闭 Memory。

把 Knowledge Retrieval 的 Query 改为：

```text
改写检索查询.rewritten_query
```

最终回答 LLM 的 User Prompt 改为：

```text
用户原始问题：{{query}}
检索时使用的改写查询：{{改写检索查询.rewritten_query}}

请回答用户原始问题。改写查询只用于检索，不能改变用户的限制条件。
```

第二版结构应为：

```text
Start
  → 改写检索查询
  → 检索文旅资料
  → 根据证据回答
  → Answer
```

### 2. 先用 Test Run 检查 Rewrite

再次运行 RW-001～010，每题使用新会话。不要修改知识库、检索参数、回答 Prompt、回答模型或 case。保存 Rewrite query、Top-K 证据和 trace ID。

填写：

| ID     | Rewrite query | 含义保留 | 是否增加事实 | Baseline 证据 | Rewrite 证据 | 完整证据变化 | 最终回答变化 | 额外耗时 | keep/revert |
| ------ | ------------- | -------- | ------------ | ------------- | ------------ | ------------ | ------------ | -------: | ----------- |
| RW-001 |               |          |              |               |              |              |              |          |             |
| RW-002 |               |          |              |               |              |              |              |          |             |
| RW-003 |               |          |              |               |              |              |              |          |             |
| RW-004 |               |          |              |               |              |              |              |          |             |
| RW-005 |               |          |              |               |              |              |              |          |             |
| RW-006 |               |          |              |               |              |              |              |          |             |
| RW-007 |               |          |              |               |              |              |              |          |             |
| RW-008 |               |          |              |               |              |              |              |          |             |
| RW-009 |               |          |              |               |              |              |              |          |             |
| RW-010 |               |          |              |               |              |              |              |          |             |

### 3. 用同一 Dataset 跑 rewrite Experiment

确认 Rewrite 版本已经发布，Dataset 仍是 `lab03-rag-trace-v1`，然后运行：

```bash
uv run python scripts/run_phoenix_lab03_eval.py \
  --experiment-name "lab03-rewrite" \
  --with-llm-judge \
  --judge-model <课堂指定的 GLM 模型> \
  --dry-run

uv run python scripts/run_phoenix_lab03_eval.py \
  --experiment-name "lab03-rewrite" \
  --with-llm-judge \
  --judge-model <课堂指定的 GLM 模型>
```

在 Phoenix 中并排比较 baseline 与 rewrite 两个 Experiment：

| 指标                         | Baseline | Rewrite | 变化 | 判断 |
| ---------------------------- | -------: | ------: | ---: | ---- |
| `answer_requirements` 平均分 |          |         |      |      |
| `evidence_complete` 平均分   |          |         |      |      |
| `grounded_answer` 平均分     |          |         |      |      |
| 失败 task run 数             |          |         |      |      |
| Dify 总 token                |          |         |      |      |
| 端到端耗时                   |          |         |      |      |

平均分只用于定位总体变化。最终 `keep / revert` 还要查看逐题结果，尤其是 RW-007 拒答、RW-009 依赖型多跳和任何 baseline 已通过但 rewrite 后失败的样本。

### 4. 三层评测

#### A. Rewrite 约束

每条样本检查：

- 关键实体是否保留；
- 数字、时间和版本是否保留；
- “全部、最晚、当前、周末、已有”等限制是否保留；
- 是否添加答案或资料中没有的事实；
- 改写后的文本能否作为独立检索查询。

任一关键限制丢失，记为失败。不要用参考改写文本的逐字匹配评分。

#### B. 检索结果

比较 baseline 和 rewrite 的：

- gold object 是否进入 Top-K；
- 必要对象是否全部进入 Context；
- 第一个相关对象的排名；
- RAG-009 是否仍保持无答案；
- RW-009 是否暴露“Rewrite 不能替代多跳分解”的边界。

#### C. 最终回答与成本

比较：

- 必答要点；
- 禁止出现的错误结论；
- 引用是否来自实际 Context；
- 新增 LLM 调用的 token 和耗时；
- 最终答案是否因改写而改善或退化。

### Rewrite 保留条件

同时满足以下条件才保留：

- 10 条样本没有关键含义丢失；
- 没有新增业务事实；
- RAG-009 仍然拒答；
- 完整证据率没有下降；
- 至少一条原本缺少必要证据的样本得到改善，或相关对象排名有可复现改善；
- 额外模型调用、token 和延迟已经记录。

如果改写只让句子更正式，却没有改善检索或答案，撤回 Rewrite 节点。

Phoenix Experiment 自动检查证据与最终回答。Rewrite query 的实体、数字、限定词和禁止新增项仍需从 Rewrite 节点 trace 检查，因为 Dify Chatflow API 的最终响应不会稳定返回该中间变量。不要用 Rewrite 文本相似度代替检索结果。

## （备选）实验 7：最终配置与回归

### 目标

整理每一步实际证据，确定最终保留的知识库、检索配置和 Rewrite 策略。

### 操作

1. 恢复最后一个通过全部质量门的检索配置。
2. 根据实验 6 的结果保留或删除 Rewrite 节点。
3. 重跑 RAG-002、004、007、009、010。
4. 打开 Dify 和 Phoenix trace，确认节点、Context、回答和引用来自最终版本。
5. 保存最终 Chatflow 版本、API key 的本地配置和 App ID；API key 不进入提交物。

填写：

| 项目                   | 最终结果             |
| ---------------------- | -------------------- |
| Chatflow App ID        |                      |
| Dify 发布版本          |                      |
| Phoenix project        |                      |
| 文本知识库 revision    |                      |
| 对象知识库 revision    |                      |
| 最终检索方式           |                      |
| 最终 Top-K / threshold |                      |
| Reranker               |                      |
| Rewrite                | keep / revert        |
| 最终冒烟 trace ID      |                      |
| 已知未通过 Case        |                      |
| Phoenix Dataset        | `lab03-rag-trace-v1` |
| Baseline Experiment    |                      |
| Rewrite Experiment     |                      |

### 最终提交物

```text
lab03-submission/
├── environment.md
├── knowledge-config.md
├── evidence-levels.md
├── rag-efficiency.tsv
├── rewrite-ab.md
├── keep-revert.md
├── dify-native-rag/
├── trace-index.md
└── phoenix-experiments.md
```

没有运行的项目写 `not_run`，不可观察的时间写 `not_observed`。不要根据教师历史结果补写个人 run ID、延迟或模型输出。

## （备选）实验 8：脚本导入与 metadata 过滤

该实验不计入主 Lab 完成条件。完成主实验并保存基线后再做，用于观察文档属性过滤对检索范围的影响。

### 1. 用脚本导入并写入原生 metadata

先完成实验 1 的环境变量配置，确认 `DIFY_BASE_URL` 和 `DIFY_KB_API_KEY` 可用，且 API key 有知识库写入权限。`XM-Guide-Text` 必须已经包含 7 个预期文件。

在仓库根目录运行；如果使用其他 Embedding 模型，将参数替换为文本库实际使用的模型标识：

```bash
uv run python scripts/sync_dify_rag_knowledge.py \
  --text-dataset XM-Guide-Text \
  --object-dataset XM-Guide-Object \
  --embedding-model baai/bge-m3
```

脚本会修改这两个知识库：核对文本库文件，为两个库批量写入并验证 metadata；对象库不存在时创建，缺少对象文件时以 Parent-child 方式上传。已存在的同名文件会跳过上传，不会更新正文；如果本地 Markdown 有修改，先在 Dify 中更新对应文档并等待索引完成。已有对象文档必须采用 Parent-child 模式。

在 Dify 的文档 metadata 面板核对字段，不能只查看 Markdown 正文：

| 字段           | 类型             | 用途                               |
| -------------- | ---------------- | ---------------------------------- |
| `revision`     | String           | 数据修订标识                       |
| `is_simulated` | String           | 模拟标记，值为字符串 `true`        |
| `valid_until`  | Time             | 文档有效期，脚本将日期转换为时间戳 |
| `document_id`  | String           | 文档标识                           |
| `page`         | Number           | 页码                               |
| `object_type`  | String，仅对象库 | 如 `table`、`flowchart`            |

若脚本报告文件缺失、字段类型不符或文档模式不符，先修复再继续；不要跳过校验。

### 2. 对比关闭过滤与有效期过滤

复制已跑通的 Chatflow，命名为 `Lab 03 Metadata 对照`，只选择 `XM-Guide-Object`。保持模型、Prompt、检索方式、Top-K、阈值和 Rerank 不变，关闭 Memory。

固定问题：

```text
截至 2026 年 8 月 23 日，轮渡在开航前 45 分钟退票，手续费是多少？
```

先关闭 Metadata Filtering，运行并记录检索结果中的文档名、页码、对象 ID 和答案。再在 Knowledge Retrieval 节点开启手动过滤（Manual），添加：

```text
字段：valid_until
条件：晚于（after）
固定日期：2026-08-23
```

使用固定日期，不使用运行当天日期，避免课程数据过期后全部被排除。重新发送同一道题，检查检索输出：2025 旧版文档的有效期为 `2026-01-31`，应被排除；2026 现行版有效期为 `2026-09-30`，仍可参与检索。预期答案为票价的 `10%`，证据应来自 `XM-REFUND-2026-P6-TABLE`。

关闭过滤时不保证旧版一定进入 Top-K；如两次结果相同，如实记录，不制造错误基线。过滤后现行表格仍未召回时，记录为检索失败，不能把“旧版被排除”当作答案正确。

### 3. 检查过度过滤

保留有效期条件，再添加 `object_type` 等于 `flowchart`，条件关系选择 AND。使用同一道退票问题运行：退票表格已被排除，答案应说明证据不足，不能凭模型记忆补出手续费。记录后移除该附加条件。

| 运行 | 过滤条件            | 返回对象 ID | 是否出现旧版 | 答案与引用 |
| ---- | ------------------- | ----------- | ------------ | ---------- |
| M0   | 关闭                |             |              |            |
| M1   | 有效期晚于固定日期  |             |              |            |
| M2   | M1 AND 类型为流程图 |             |              |            |

### 通过条件与恢复

- 确认原生 metadata 已写入，并保存手动过滤设置与检索结果。
- M1 不返回旧版退票文档；M2 不返回退票表格，缺少证据时明确拒答。
- 对比记录独立保存，不混入 D0–D4 或 Rewrite A/B 的基线结果。
- 后续主实验继续使用原 Chatflow，保持 metadata 过滤关闭。

本实验的有效期字段用于演示版本筛选，不替代完整的生效区间判断或访问权限校验。Dify 的手动 metadata 过滤说明见[官方文档](https://github.com/langgenius/dify-docs/blob/main/en/cloud/use-dify/nodes/knowledge-retrieval.mdx)。

## 扩展实验：依赖型两跳 RAG

该实验不计入主 Lab 完成条件。只有主流程和 Rewrite A/B 已完成时再做。

另建最小 Workflow：

```text
Start
  → LLM（生成第一跳查询）
  → Knowledge Retrieval 1
  → LLM（读取第一跳实体，生成第二跳查询）
  → Knowledge Retrieval 2
  → LLM（合并两跳证据）
  → End
```

固定问题：

```text
室内无障碍场馆里，周一开放的场馆最晚几点停止入馆？
```

唯一核心验收项：第二跳实际 query 必须包含第一跳检索得到的场馆实体。两次独立检索后由最终模型拼接答案，不能算作依赖型执行。

停止条件：第一跳没有取得实体、第二跳没有使用第一跳实体、任一必要证据缺失，或系统仍生成确定答案。
