"""全文复制到人工决定后的 Code 节点；DECISION 由节点代码固定。"""

import json

DECISION = "approve"  # 另三份节点分别改为 apply_edit、cancel、timeout


def main(state_json: str, reviewed_answer: str = "") -> dict:
    state = json.loads(state_json)
    if state["outcome"] != "pending_review" or not state["action_id"]:
        raise ValueError("没有对应的待审批动作")
    outcomes = {
        "approve": "approved",
        "apply_edit": "edited",
        "cancel": "cancelled",
        "timeout": "timed_out",
    }
    if DECISION not in outcomes:
        raise ValueError("未知人工决定")
    if DECISION == "apply_edit":
        if not reviewed_answer.strip():
            raise ValueError("修改后采用需要非空的修改文本")
        state["answer"] = reviewed_answer.strip()
    elif DECISION == "cancel":
        state["answer"] = "已取消本次候选动作，未保存行程。"
    elif DECISION == "timeout":
        state["answer"] = "等待确认已超时，未保存行程。"
    state["human_decision"] = DECISION
    state["outcome"] = outcomes[DECISION]
    # 这些节点只记录决定，没有调用业务写接口。
    return {"state_json": json.dumps(state, ensure_ascii=False)}
