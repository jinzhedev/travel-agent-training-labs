from __future__ import annotations

import argparse
import json
from pathlib import Path

from training_eval.rag import DEFAULT_CASES, score_rag_predictions, validate_rag_case_set


def main() -> int:
    parser = argparse.ArgumentParser(description="校验 CP03 文档 RAG 金标，或评估预测")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--predictions", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result: dict[str, object] = {"dataset": validate_rag_case_set(args.cases)}
    if args.predictions:
        result["prediction_score"] = score_rag_predictions(args.predictions, args.cases)
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result["dataset"]["status"] == "ok" else 1  # type: ignore[index]


if __name__ == "__main__":
    raise SystemExit(main())
