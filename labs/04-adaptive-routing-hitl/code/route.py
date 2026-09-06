"""全文复制到 Dify 的“校验与路由”Code 节点；只使用标准库。"""

import json
from datetime import date


def main(request: dict) -> dict:
    if not isinstance(request, dict):
        raise ValueError("结构化请求必须是 Object")
    tasks = request.get("tasks")
    missing = request.get("missing_information")
    action = request.get("proposed_action")
    if not isinstance(tasks, list) or not isinstance(missing, list):
        raise ValueError("tasks / missing_information 必须是数组")
    if action not in ("none", "save_itinerary"):
        raise ValueError("未知候选动作")
    if not all(isinstance(item, str) for item in missing):
        raise ValueError("missing_information 元素必须是字符串")
    unsupported = request.get("unsupported_reason")
    candidate = request.get("candidate_text")
    if not isinstance(unsupported, str) or not isinstance(candidate, str):
        raise ValueError("unsupported_reason / candidate_text 必须是字符串")
    groups = {"travel": [], "life": []}
    seen = set()
    missing = list(missing)
    for task in tasks:
        if not isinstance(task, dict) or task.get("kind") not in ("weather", "poi", "utility"):
            raise ValueError("未知任务类型")
        if not all(
            isinstance(task.get(key), str)
            for key in ("id", "query", "city", "date", "utility_type")
        ):
            raise ValueError("任务字段类型不正确")
        if not task["id"] or task["id"] in seen or not task["query"].strip():
            raise ValueError("任务 ID 重复、为空或子任务没有描述")
        seen.add(task["id"])
        deps = task.get("depends_on")
        if not isinstance(deps, list) or not all(isinstance(item, str) for item in deps):
            raise ValueError("depends_on 必须是字符串数组")
        if deps:
            unsupported = "本流程只执行独立子任务，请把有依赖的任务分开提交。"
        if not task["city"].strip():
            missing.append("城市")
        if task["kind"] in ("weather", "poi"):
            try:
                if date.fromisoformat(task["date"]).isoformat() != task["date"]:
                    raise ValueError
            except ValueError:
                missing.append("具体日期（YYYY-MM-DD）")
        elif task["utility_type"] not in ("water", "electricity"):
            missing.append("水费还是电费")
        owner = "life" if task["kind"] == "utility" else "travel"
        groups[owner].append(task)
    if len(groups["travel"]) > 1 or len(groups["life"]) > 1:
        unsupported = "本流程每次最多处理一个旅游查询和一个生活服务查询。"
    if action == "save_itinerary" and not candidate.strip() and not tasks:
        missing.append("待保存的行程文本")
    missing = list(dict.fromkeys(missing))
    if unsupported:
        route, message = "unsupported", unsupported
    elif missing:
        route, message = "clarify", "请补充：" + "、".join(missing)
    elif groups["travel"] and groups["life"]:
        route, message = "both", ""
    elif groups["travel"]:
        route, message = "travel", ""
    elif groups["life"]:
        route, message = "life", ""
    else:
        route, message = "direct", candidate.strip() or "不客气。"
    active = route not in ("clarify", "unsupported")
    state = {
        "route": route,
        "tasks": tasks,
        "missing_information": missing,
        "action": action if active else "none",
        "candidate_text": candidate.strip(),
        "message": message,
    }
    return {
        "route": route,
        "need_travel": active and bool(groups["travel"]),
        "need_life": active and bool(groups["life"]),
        "travel_query": json.dumps(groups["travel"], ensure_ascii=False) if active else "[]",
        "life_query": json.dumps(groups["life"], ensure_ascii=False) if active else "[]",
        "state_json": json.dumps(state, ensure_ascii=False),
    }
