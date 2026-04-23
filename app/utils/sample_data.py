from __future__ import annotations

import json
from pathlib import Path
from typing import Any


SAMPLE_DIR = Path(__file__).resolve().parents[2] / "sample_data"


def load_sample(source: str) -> list[dict[str, Any]]:
    path = SAMPLE_DIR / f"{source}.json"
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)
