# 基线配置快照

manifest 从原 reports/local/lab06/baseline.manifest.json 迁入，参数未修改。

workflow.dify.yml 复制自 reports/local/Lab 06-202609082301.yml。prompts/ 从该 DSL 的 Agent 指令、最终 LLM System / User Prompt 和查询模板提取，保留 Dify 变量标记。

文件名标记导出时间为 2026-09-08 23:01；尚未核对实际发布日期及当前线上配置。运行前核对 manifest、此快照与已发布应用是否一致。

Judge 副本来自课程 judge-v1.txt，位于 ../evaluators/，运行命令通过 --judge-prompt 显式指定。
