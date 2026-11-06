"""Written remarks from the judge for each chat answer: what's good, what's bad, what to improve.

Based on the question, the answer, the correct result, the code-check results and the judge's
own scores, so the remarks agree with the numbers. One judge call per answer.
"""
from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from pydantic import BaseModel, Field


class Remarks(BaseModel):
    whats_good: list[str] = Field(description="What the answer did well. Short, specific points.")
    whats_bad: list[str] = Field(description="What is wrong, missing or misleading. Empty if nothing.")
    improvements: list[str] = Field(description="Concrete changes that would make the answer better. "
                                                "Empty if nothing worth changing.")


PROMPT = """You are reviewing one answer from a data-analysis chat assistant for an electricity utility.
The assistant writes SQL against masked table/column names, the result table is shown to the user
separately, and the assistant itself only sees counts and summary figures (never the row values).

Write remarks in three separate lists:
- whats_good: what the answer did well
- whats_bad: what is wrong, missing or misleading (empty list if nothing)
- improvements: concrete changes that would make the answer better (empty list if nothing)

Rules:
- Base every point on the evidence below; do not invent problems. Agree with the code checks and scores.
- Be specific (name the number, filter, wording or step), one short sentence per point, at most 4 points per list.
- Judge the written answer AND the data shown (the code check "data_correct" says whether the table was right).
- Improvements should say what the assistant should do differently, not repeat the problem.

QUESTION:
{question}

ANSWER (as the user reads it; tables and follow-up buttons are shown separately):
{answer}

CORRECT RESULT / REQUIRED BEHAVIOUR:
{expected}

SQL THE ASSISTANT RAN (masked names):
{sql}

CODE CHECKS:
{checks}

JUDGE SCORES (0-1) AND REASONS:
{scores}
"""


def _checks_text(turn: dict[str, Any]) -> str:
    # Governance checks (real names / sensitive values reaching the model) are an app-level setting,
    # not a quality of this answer, and are reported separately; they are left out here.
    lines = []
    for c in turn.get("checks", []):
        lines.append(f"- {c['name']}: {'PASS' if c['passed'] else 'FAIL'} ({c.get('detail', '')})")
    return "\n".join(lines) or "(none)"


def _scores_text(scores: dict[str, Any]) -> str:
    lines = []
    for name, v in (scores or {}).items():
        if v.get("score") is None:
            continue
        lines.append(f"- {name}: {v['score']:.2f}. {(v.get('reason') or '').strip()}")
    return "\n".join(lines) or "(not judged)"


def remarks_for_turn(judge, turn: dict[str, Any], scores: dict[str, Any]) -> tuple[dict[str, Any], float]:
    ji = turn.get("judge_input") or {}
    prompt = PROMPT.format(
        question=turn.get("question", ""),
        answer=turn.get("answer") or "(no answer)",
        expected=ji.get("expected_output") or turn.get("expected_behavior") or "(none)",
        sql="\n".join(turn.get("sql") or []) or "(no query run)",
        checks=_checks_text(turn),
        scores=_scores_text(scores),
    )
    try:
        out, cost = judge.generate(prompt, schema=Remarks)
        if isinstance(out, str):  # schema not honoured: parse JSON text
            out = Remarks.model_validate(json.loads(out))
        return out.model_dump(), float(cost or 0.0)
    except Exception as exc:
        return {"whats_good": [], "whats_bad": [], "improvements": [], "error": f"{type(exc).__name__}: {exc}"}, 0.0


def add_remarks(judge, records: dict[str, dict[str, Any]], scores: dict[str, dict[str, Any]],
                workers: int = 4) -> float:
    """Fills turn['remarks'] for every turn of every record (in place). Returns the judge cost."""
    jobs = [(t, scores.get(t["name"], {})) for rec in records.values() if not rec.get("error") for t in rec["turns"]]
    total = 0.0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for (turn, _), (remarks, cost) in zip(jobs, pool.map(lambda j: remarks_for_turn(judge, *j), jobs)):
            turn["remarks"] = remarks
            total += cost
    return total


def print_remarks(records: dict[str, dict[str, Any]]) -> None:
    for rec in records.values():
        for t in rec.get("turns", []):
            r = t.get("remarks")
            if not r:
                continue
            print(f"\n{t['name']}  {t['question']}")
            if r.get("error"):
                print(f"  (remarks failed: {r['error'][:120]})")
                continue
            for title, key in (("Good", "whats_good"), ("Bad", "whats_bad"), ("Improve", "improvements")):
                items = r.get(key) or []
                print(f"  {title}:" + (" -" if not items else ""))
                for item in items:
                    print(f"    * {item}")
