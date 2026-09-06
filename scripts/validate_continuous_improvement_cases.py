from __future__ import annotations

import argparse
import json
from pathlib import Path

from training_eval.continuous_improvement import (
    DEFAULT_CALIBRATION,
    DEFAULT_CANDIDATES,
    DEFAULT_CASES,
    DEFAULT_TRACES,
    score_curation_predictions,
    score_release_decisions,
    validate_continuous_improvement_dataset,
)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="校验 Day 3 PM 持续改进数据和可选学员判断"
    )
    parser.add_argument("--traces", type=Path, default=DEFAULT_TRACES)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--calibration", type=Path, default=DEFAULT_CALIBRATION)
    parser.add_argument("--candidates", type=Path, default=DEFAULT_CANDIDATES)
    parser.add_argument("--curation-predictions", type=Path)
    parser.add_argument("--release-decisions", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    dataset = validate_continuous_improvement_dataset(
        args.traces, args.cases, args.calibration, args.candidates
    )
    result: dict[str, object] = {"dataset": dataset}
    if dataset["status"] != "ok":
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 1
    if args.curation_predictions:
        result["curation"] = score_curation_predictions(
            args.curation_predictions, args.cases
        )
    if args.release_decisions:
        result["release"] = score_release_decisions(
            args.release_decisions, args.candidates
        )

    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
