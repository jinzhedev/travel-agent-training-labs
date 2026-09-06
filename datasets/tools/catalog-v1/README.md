# 工具目录 v1

`catalog.json` 是 CP02 工具定义的唯一权威来源。Travel Core、Dify checkpoint、Lab 和评测器都读取或引用这份目录，不在各自目录复制 Tool Schema。

目录包含 32 个课堂工具，覆盖景点、天气、交通、住宿、行程、预订、生活服务、账户和运维 namespace。多数工具是冻结数据或模拟执行器；不会发生真实预订、支付、取消、账户修改或生产写入。

每张工具卡包含：

- namespace、名称和用途；
- `use_when`、`avoid_when` 和检索关键词；
- 所需权限、风险等级、副作用和确认要求；
- 延迟档位与 JSON Schema；
- 版本、环境和模拟边界。

数据修订为 `tool-catalog-v1.5`。`poi.search` 返回用于展示的景点名称和用于内部关联的规范 `poi_id`；`itinerary.build_draft` 可以接收 `candidate_poi_ids / must_visit_poi_ids / exclude_poi_ids`，执行层按 ID 回查权威 POI 数据，不信任模型回传名称、开放时间或票价。`itinerary.save` 的用户身份来自鉴权上下文，模型参数只保留当前行程和明确确认；执行层还会核对当前行程与可信 Workflow 状态。

本次修订补充了实时事实路由和轮渡模拟契约：天气检索覆盖“下雨/降雨”等表达，`mobility.search_ferry` 支持可选的 `departure_time` 与 `passenger_types`，返回 `sailings`、指定班次的 `remaining` 和 `passenger_inventory`。数据来自同目录的 `ferry-status.json`，仍是课堂模拟库存，不代表真实票务系统。

该目录用于比较全量工具、namespace 过滤和候选工具检索三种策略，不代表任何真实平台或第三方服务的 API。
