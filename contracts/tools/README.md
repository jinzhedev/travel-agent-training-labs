# 工具定义与调用约束

CP02 使用三层契约：

1. `tool-catalog.schema.json`：约束工具目录和工具卡；
2. 每张工具卡的 `input_schema`：约束具体工具参数；
3. Travel Core 的 OpenAPI：约束目录查询、候选选择和批量执行接口。

工具目录的唯一实例位于 `datasets/tools/catalog-v1/catalog.json`。Dify 只消费 Travel Core 返回的候选卡片，不另存一份 Tool Schema。

执行顺序固定为：

```text
可信权限上下文
  → 环境与权限硬过滤
  → namespace / 候选工具检索
  → 模型生成 action 与 calls
  → 候选集合检查和参数 Schema 校验
  → 执行时再次鉴权
  → 高风险工具检查 approval_id
  → 幂等执行与审计字段
```

工具检索负责“找得到”，权限系统决定“能否看见、能否执行”。模型输出不能扩大权限。
