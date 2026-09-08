"""复制到每个 Agent 后的 Code 节点；steps 绑定 Agent.json。"""


def main(steps: list) -> dict:
    rounds = [
        step
        for step in steps
        if isinstance(step, dict)
        and step.get("parent_id") is None
        and str(step.get("label", "")).startswith("ROUND ")
    ]
    if not rounds:
        raise ValueError("Agent 没有提供可核对的轮次结果")
    last = rounds[-1]
    data = last.get("data")
    output = data.get("output") if isinstance(data, dict) else None
    if (
        last.get("status") != "success"
        or not isinstance(output, dict)
        or "tool_responses" not in output
        or output["tool_responses"]
    ):
        raise ValueError("Agent 最后一轮尚未完成最终回答")
    answer = output.get("llm_response")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("Agent 最终回答为空")
    return {"text": answer.strip()}
