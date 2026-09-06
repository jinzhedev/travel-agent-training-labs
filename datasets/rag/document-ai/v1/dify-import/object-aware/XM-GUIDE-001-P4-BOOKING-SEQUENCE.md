# 图 2 票务占位与人工确认交互

- object_id: `XM-GUIDE-001-P4-BOOKING-SEQUENCE`
- document_id: `XM-GUIDE-001`
- page: `4`
- object_type: `sequence_diagram`
- bbox: `[30, 127, 565, 720]`
- fine_grained_ids: `["P4-S1-E3", "P4-S1-E4", "P4-S1-E6", "P4-S1-E7"]`
- observed_at: `2026-08-23`
- valid_until: `2026-09-30`
- revision: `xiamen-document-ai-v1.0.1`
- is_simulated: `true`

## 对象内容

1. 用户 → Dify：提出含票务的行程需求；条件：无
2. Dify → Travel Core：请求只读报价；条件：无
3. Travel Core → Booking Mock：创建短时占位；条件：报价仍有效
4. Dify → Human Input：展示价格与退改规则；条件：存在副作用
5. Human Input → Dify：批准或拒绝；条件：无
6. Dify → Travel Core：提交或取消占位；条件：携带 approval_id
7. Travel Core → Booking Mock：查询最终状态；条件：提交超时或返回不确定

### 时序事件

- `P4-S1-E1`：用户 → Dify；提出含票务的行程需求；条件：无
- `P4-S1-E2`：Dify → Travel Core；请求只读报价；条件：无
- `P4-S1-E3`：Travel Core → Booking Mock；创建短时占位；条件：报价仍有效
- `P4-S1-E4`：Dify → Human Input；展示价格与退改规则；条件：存在副作用
- `P4-S1-E5`：Human Input → Dify；批准或拒绝；条件：无
- `P4-S1-E6`：Dify → Travel Core；提交或取消占位；条件：携带 approval_id
- `P4-S1-E7`：Travel Core → Booking Mock；查询最终状态；条件：提交超时或返回不确定
