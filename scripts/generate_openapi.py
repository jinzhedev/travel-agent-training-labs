from __future__ import annotations

import argparse
import json
from pathlib import Path

from travel_core.main import app

DEFAULT_OUTPUT = (
    Path(__file__).resolve().parents[1]
    / "contracts/generated/travel-core.openapi.json"
)


def main() -> None:
    parser = argparse.ArgumentParser(description="从 Travel Core 代码生成 OpenAPI")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(app.openapi(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(args.output)


if __name__ == "__main__":
    main()
