"""全文复制到“合并结果”Code 节点。"""

import json


def main(state_json: str, travel_answer: str, life_answer: str, run_id: str) -> dict:
    state = json.loads(state_json)
    route = state["route"]
    required = {"travel": route in ("travel", "both"), "life": route in ("life", "both")}
    answers = {"travel": travel_answer.strip(), "life": life_answer.strip()}
    for owner, needed in required.items():
        if needed != bool(answers[owner]):
            raise ValueError(f"{owner} 执行结果缺失或出现了未分配的结果")
    pieces = (
        [state["candidate_text"]]
        if state["candidate_text"] and route not in ("clarify", "unsupported")
        else []
    )
    pieces.extend(f"{owner}：{answers[owner]}" for owner in ("travel", "life") if required[owner])
    answer = "\n\n".join(pieces) or state["message"]
    needs_review = state["action"] != "none"
    if not run_id or not answer:
        raise ValueError("缺少 run ID 或候选文本")
    state.update(
        {
            "workflow_run_id": run_id,
            "worker_answers": {owner: value for owner, value in answers.items() if required[owner]},
            "answer": answer,
            "action_id": f"{run_id}:save_itinerary" if needs_review else "",
            "outcome": "pending_review"
            if needs_review
            else (route if route in ("clarify", "unsupported") else "auto_completed"),
            "side_effect_count": 0,
        }
    )
    return {
        "state_json": json.dumps(state, ensure_ascii=False),
        "answer": answer,
        "needs_review": needs_review,
        "action_id": state["action_id"],
    }
