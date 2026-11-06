"""Local result files (the full record) and the masked view sent to the dashboard.

Local files hold everything, including real rows and raw tool output. They are
the main record and never leave this machine.

The masked view holds only what the app's models themselves saw (anonymized
names, masked SQL, row counts/KPI summaries instead of rows) plus scores.
"""
from __future__ import annotations

import dataclasses
import json
import re
import time
from pathlib import Path
from typing import Any

from evals.config import RESULTS_DIR

# Arguments the chat injects server-side; not part of what the model chose.
_INJECTED_ARGS = {"tenant_id", "organization_id", "user_id"}


class RunWriter:
    """results/<dataset>/<timestamp>_<phase>/ : one JSON per case + summary.json."""

    def __init__(self, dataset: str, phase: str):
        self.run_id = time.strftime("%Y%m%d-%H%M%S") + f"_{phase}"
        self.dir = RESULTS_DIR / dataset / self.run_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dataset = dataset
        self.phase = phase

    def write_case(self, case_id: str, payload: dict[str, Any]) -> Path:
        path = self.dir / f"{case_id}.json"
        path.write_text(json.dumps(_plain(payload), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return path

    def write_summary(self, payload: dict[str, Any]) -> Path:
        path = self.dir / "summary.json"
        body = {"dataset": self.dataset, "phase": self.phase, "run_id": self.run_id, **payload}
        path.write_text(json.dumps(_plain(body), indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return path


def _plain(obj: Any) -> Any:
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    return obj


_UI_BLOCK = re.compile(r"```(?:table|chart|followups)\b.*?```", re.DOTALL)


def answer_for_judge(answer: str) -> str:
    """The answer as the user reads it: UI blocks (table/chart placeholders, follow-up
    suggestion buttons) are rendered by the frontend, not read as prose, so the judge
    shouldn't grade them as statements. The raw answer is kept in the local trace."""
    return re.sub(r"\n{3,}", "\n\n", _UI_BLOCK.sub("", answer or "")).strip()


def masked_tool_calls(trace: dict[str, Any]) -> list[dict[str, Any]]:
    """Tool calls as the model saw them: its own arguments and the result it received."""
    out = []
    for tc in trace.get("tool_calls", []):
        out.append({
            "name": tc["name"],
            "arguments": {k: v for k, v in (tc.get("arguments") or {}).items() if k not in _INJECTED_ARGS},
            "result_seen_by_model": tc.get("result_seen_by_model"),
            "is_error": tc.get("is_error"),
            "duration_ms": tc.get("duration_ms"),
        })
    return out


def usage_totals(trace: dict[str, Any]) -> dict[str, int]:
    totals = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
    for call in trace.get("llm_calls", []):
        usage = (call.get("response") or {}).get("usage") or {}
        for key in totals:
            totals[key] += int(usage.get(key) or 0)
    # input_tokens alone is only the uncached part; this is everything the model read.
    totals["total_input_tokens"] = (totals["input_tokens"] + totals["cache_read_input_tokens"]
                                    + totals["cache_creation_input_tokens"])
    totals["llm_calls"] = len(trace.get("llm_calls", []))
    return totals
