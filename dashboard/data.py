"""Read evaluation runs and golden datasets from disk for the dashboard (no Django needed)."""
from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parents[1]
if str(EVALS_DIR) not in sys.path:
    sys.path.insert(0, str(EVALS_DIR))

from evals.config import DATASETS_DIR, RESULTS_DIR  # noqa: E402

THRESHOLD = 0.5

# Result-folder suffix -> evaluation kind (old "phaseN" names included).
KIND_BY_SUFFIX = {
    "phase0-smoke": "smoke", "smoke-test": "smoke",
    "phase1-tools": "tools", "tools": "tools",
    "phase2-chat": "chat", "chat": "chat",
    "phase3-schema": "schema-context", "schema-context": "schema-context",
}
KIND_LABEL = {"chat": "Chat responses", "tools": "Tools", "schema-context": "Schema context", "smoke": "Smoke test"}

METRIC_LABEL = {
    "Correctness [GEval]": "Correctness",
    "Answer Relevancy": "Answer Relevancy",
    "Faithfulness": "Faithfulness",
    "Knowledge Retention": "Knowledge Retention",
    "Conversation Completeness": "Conversation Completeness",
}
TURN_METRICS = ["Correctness [GEval]", "Answer Relevancy", "Faithfulness"]
CONV_METRICS = ["Knowledge Retention", "Conversation Completeness"]


@dataclass
class Run:
    dataset: str
    run_id: str
    path: Path
    kind: str
    started: datetime | None
    summary: dict[str, Any] = field(default_factory=dict)

    @property
    def label(self) -> str:
        when = self.started.strftime("%d %b %Y, %H:%M") if self.started else self.run_id
        return f"{KIND_LABEL.get(self.kind, self.kind)} · {self.dataset} · {when}"


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def list_runs() -> list[Run]:
    runs = []
    if not RESULTS_DIR.exists():
        return runs
    for ds_dir in RESULTS_DIR.iterdir():
        if not ds_dir.is_dir() or ds_dir.name.startswith("_"):
            continue
        for run_dir in ds_dir.iterdir():
            if not run_dir.is_dir() or "_" not in run_dir.name:
                continue
            stamp, suffix = run_dir.name.split("_", 1)
            kind = KIND_BY_SUFFIX.get(suffix)
            if not kind:
                continue
            try:
                started = datetime.strptime(stamp, "%Y%m%d-%H%M%S")
            except ValueError:
                started = None
            summary = _read_json(run_dir / "summary.json") or {}
            runs.append(Run(ds_dir.name, run_dir.name, run_dir, kind, started, summary))
    runs.sort(key=lambda r: r.started or datetime.min, reverse=True)
    return runs


def load_cases(run: Run) -> dict[str, dict[str, Any]]:
    """Every per-case JSON of a run (summary, profile and logs excluded)."""
    out = {}
    for p in sorted(run.path.glob("*.json")):
        if p.name in ("summary.json", "profile.json"):
            continue
        data = _read_json(p)
        if isinstance(data, dict):
            if run.kind == "chat":
                _normalize_chat(data, p.stem)
            out[p.stem] = data
    return out


def _normalize_chat(rec: dict[str, Any], case_id: str) -> None:
    """Older chat runs saved turns without names/ids; fill them so every run displays."""
    rec.setdefault("id", case_id)
    turns = rec.get("turns") or []
    for i, t in enumerate(turns, 1):
        t.setdefault("name", case_id + (f"-t{i}" if len(turns) > 1 else ""))
        t.setdefault("question", "")
        t.setdefault("checks", [])
        t.setdefault("governance", [])


# ---------------------------------------------------------------------------
# Chat runs
# ---------------------------------------------------------------------------

def metric_ok(v: dict[str, Any] | None) -> bool | None:
    if not v or v.get("score") is None:
        return None
    return bool(v.get("passed")) if v.get("passed") is not None else v["score"] >= THRESHOLD


def chat_case_status(rec: dict[str, Any]) -> str:
    """Passed = every code check passes and every judge metric passes (governance reported separately)."""
    if rec.get("error"):
        return "Errored"
    for t in rec.get("turns", []):
        if not all(c.get("passed") for c in t.get("checks", [])):
            return "Failed"
        for v in (t.get("judge") or {}).values():
            if metric_ok(v) is False:
                return "Failed"
    for v in (rec.get("conversation_judge") or {}).values():
        if metric_ok(v) is False:
            return "Failed"
    return "Passed"


def case_avg_score(rec: dict[str, Any]) -> float | None:
    vals = [v["score"] for t in rec.get("turns", []) for v in (t.get("judge") or {}).values()
            if v and v.get("score") is not None]
    vals += [v["score"] for v in (rec.get("conversation_judge") or {}).values() if v and v.get("score") is not None]
    return sum(vals) / len(vals) if vals else None


def chat_metric_scores(records: dict[str, dict[str, Any]]) -> dict[str, list[tuple[str, float, bool]]]:
    """metric -> [(test name, score, passed)]."""
    out: dict[str, list[tuple[str, float, bool]]] = {}
    for rec in records.values():
        for t in rec.get("turns", []):
            for m, v in (t.get("judge") or {}).items():
                if v and v.get("score") is not None:
                    out.setdefault(m, []).append((t["name"], v["score"], bool(metric_ok(v))))
        for m, v in (rec.get("conversation_judge") or {}).items():
            if v and v.get("score") is not None:
                out.setdefault(m, []).append((rec["id"], v["score"], bool(metric_ok(v))))
    return out


# ---------------------------------------------------------------------------
# Datasets / goldens
# ---------------------------------------------------------------------------

def list_datasets() -> list[str]:
    return sorted(p.parent.name for p in DATASETS_DIR.glob("*/dataset.toml"))


def golden_path(dataset: str, kind: str) -> Path:
    """One golden file per evaluation: goldens/chat.yaml, goldens/tools.yaml (later: schema_context.yaml)."""
    return DATASETS_DIR / dataset / "goldens" / f"{kind}.yaml"


def judge_model_name() -> str:
    return os.getenv("JUDGE_MODEL", "claude-opus-5-5")
