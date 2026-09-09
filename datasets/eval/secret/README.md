# Lab 06：secret 最终测试集

本目录包含 8 条课程编写的最终测试案例，ID 为 `L06-S001` 至 `L06-S008`。Phoenix Dataset 名称使用 `secret`。公开的 12 条开发案例用于定位错误、修改应用和校准 Judge；本集在基线、候选及 Judge 配置冻结后运行。

这是课堂留出测试演示，不是有访问隔离的盲测。问题和验收条件随仓库提供，学员可以读取；课堂约定在最终测试前不打开案例文件、不运行本集、不根据它修改方案。已经阅读或用过这些案例的人，应将其视为回归集，不能声称对自己仍是未见测试。案例与开发集共享业务规则和冻结事实，结果不证明对新业务场景的泛化能力。

## 数据与边界

`cases.jsonl` 是本集问题和验收条件的权威来源。覆盖入馆截止时间、不同场馆的开放规则、分组出行、室内设施证据范围、天气查询、取消退款能力边界和缺少日期。这里没有预设应用回答一定为 pass 或 fail；正负例须由实际回答和人工复核确定。

不复制文旅事实。准备脚本从 `datasets/rag/document-ai/v1/parsed/object-aware.jsonl` 引用文档对象，从 Travel Core 天气实现生成对应日期的冻结参考，沿用现有来源、有效期、模拟标记、revision 和来源哈希。全部为课程模拟场景，订单标识也是虚构示例。

## 最终测试

冻结配置后，在仓库根目录执行：

```bash
uv run python scripts/run_phoenix_lab06_eval.py prepare \
  --cases datasets/eval/secret/cases.jsonl \
  --dataset-name secret --apply
```

保存输出的 `dataset_version`。基线与候选必须使用同一 `secret` 版本、同一 Judge 和相同 trial 数，各自恢复并发布对应的 Dify 配置后运行。操作及人工复核命令见 [Lab 06 最终测试](../../../labs/06-evals-release/LAB.md#3-secret-最终测试)。

最终结果单独保存，不与公开开发集的报告混用。看到结果后再修改应用或 Judge，本集就已参与开发；后续独立验证需要新的未使用案例。同名 Phoenix Dataset 已有不同内容时，准备脚本会停止；不要覆盖旧版本，可另用 `secret-v2` 保存下一轮测试集。
