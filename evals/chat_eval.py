"""Chat evaluation: golden questions through the real in-app chat, graded by code checks + the Opus judge."""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

import yaml

from evals import checks
from evals.config import Dataset
from evals.tools_eval import Reference, _norm

NUM_TOLERANCE = 0.51  # absolute; covers rounding to whole numbers / 2 decimals. Counts compare exactly anyway.


# ---------------------------------------------------------------------------
# Golden cases
# ---------------------------------------------------------------------------

@dataclass
class GoldenTurn:
    question: str
    expected_sql: str | None = None
    match: str = "rows"                 # rows | contains | none
    expected_behavior: str | None = None
    expected_answer: str | None = None       # the answer a good assistant gives (from the CSV)
    labels: dict[str, list[str]] = field(default_factory=dict)  # code -> words the chat may show instead
    expect: dict[str, Any] = field(default_factory=dict)        # feature expectations (chart, report, ...)
    expected: dict[str, Any] | None = None   # filled from the reference: {"columns", "rows"}


@dataclass
class GoldenCase:
    id: str
    turns: list[GoldenTurn]
    notes: str = ""
    user: str = "full_access"
    level: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def multi_turn(self) -> bool:
        return len(self.turns) > 1

    @property
    def kind(self) -> str:
        if self.multi_turn:
            return "multi-turn"
        return "behaviour" if self.turns[0].match == "none" else "data"


def load_cases(ds: Dataset, filename: str = "chat.yaml") -> list[GoldenCase]:
    """The cases in the dataset's goldens/chat.yaml."""
    raw = yaml.safe_load((ds.goldens_dir / filename).read_text(encoding="utf-8"))
    out = []
    for c in raw["cases"]:
        case_labels = c.get("labels") or {}
        turns = [GoldenTurn(question=t["question"], expected_sql=t.get("expected_sql"),
                            match=t.get("match", "rows" if t.get("expected_sql") else "none"),
                            expected_behavior=t.get("expected_behavior"),
                            expected_answer=t.get("expected_answer"),
                            labels={str(k): [str(x) for x in v] for k, v in {**case_labels, **(t.get("labels") or {})}.items()},
                            expect=dict(t.get("expect") or {}))
                 for t in c["turns"]]
        out.append(GoldenCase(id=c["id"], turns=turns, notes=" ".join(str(c.get("notes", "")).split()),
                              user=c.get("user", "full_access"), level=c.get("level", ""),
                              tags=list(c.get("tags") or [])))
    return out


def fill_expected(cases: list[GoldenCase], ref: Reference) -> None:
    for case in cases:
        for t in case.turns:
            if t.expected_sql:
                cols, rows = ref.run(" ".join(t.expected_sql.split()))
                t.expected = {"columns": cols, "rows": rows}


# ---------------------------------------------------------------------------
# Data check: does the table shown to the user hold the expected values?
# ---------------------------------------------------------------------------

def _cell(v: Any) -> Any:
    v = _norm(v)
    return v.lower() if isinstance(v, str) else v


_MONTHS = ["january", "february", "march", "april", "may", "june", "july", "august", "september",
           "october", "november", "december"]


def _month_variants(text: str) -> set[str] | None:
    """'04-2026' -> the ways a month may be written: 2026-04, april 2026, apr-2026, ..."""
    m = re.fullmatch(r"(\d{2})-(\d{4})", text)
    if not m or not 1 <= int(m.group(1)) <= 12:
        return None
    mm, yyyy = m.group(1), m.group(2)
    full = _MONTHS[int(mm) - 1]
    out = {f"{mm}-{yyyy}", f"{yyyy}-{mm}", f"{mm}/{yyyy}", f"{yyyy}/{mm}", f"{yyyy}-{mm}-01"}
    for name in (full, full[:3]):
        out |= {f"{name} {yyyy}", f"{name}-{yyyy}", f"{name}, {yyyy}", f"{name}'{yyyy[2:]}", f"{name}-{yyyy[2:]}"}
    return out


def _same(a: Any, b: Any, labels: dict[Any, set[str]] | None = None) -> bool:
    """a = actual cell, b = expected cell (both normalised by _cell)."""
    if isinstance(a, float) and isinstance(b, float):
        if abs(a - b) <= NUM_TOLERANCE or (b != 0 and abs(a - b) / abs(b) <= 1e-9):
            return True
    elif a == b:
        return True
    if isinstance(a, str):
        if labels and b in labels and a.strip() in labels[b]:
            return True
        if isinstance(b, str):
            variants = _month_variants(b)
            if variants and a.strip() in variants:
                return True
    return False


def _row_holds(actual: list[Any], expected: list[Any], labels: dict[Any, set[str]] | None = None) -> bool:
    """Every expected value is found in a different cell of the actual row (extra columns allowed)."""
    pool = [_cell(v) for v in actual]
    for e in expected:
        e = _cell(e)
        hit = next((i for i, a in enumerate(pool) if _same(a, e, labels)), None)
        if hit is None:
            return False
        pool.pop(hit)
    return True


def _numbers_in_text(text: str) -> list[float]:
    out = []
    for m in re.findall(r"-?\d[\d,]*(?:\.\d+)?", text or ""):
        try:
            out.append(float(m.replace(",", "")))
        except ValueError:
            pass
    return out


def check_data(turn: GoldenTurn, ui_tables: list[dict[str, Any]], answer: str) -> checks.CheckResult:
    if turn.match == "none" or not turn.expected:
        return checks.CheckResult("data_correct", True, "no data check for this turn")
    # Rows with an empty (NULL) value are optional: the chat may filter them out.
    exp_rows = turn.expected["rows"]
    required = [r for r in exp_rows if all(v is not None for v in r)]
    if not required:
        return checks.CheckResult("data_correct", True, "expected result is empty/NULL only")

    labels = {_cell(k): {str(x).strip().lower() for x in v} for k, v in (turn.labels or {}).items()}

    def as_rows(t):
        return [list(r.values()) if isinstance(r, dict) else list(r) for r in (t.get("rows") or [])]

    # Each table on its own; then all tables together (the chat may split one answer over
    # several tables, e.g. billed and unbilled counts as two one-row tables).
    candidates = [(t.get("id", "?")[-6:], as_rows(t), True) for t in ui_tables]
    if len(ui_tables) > 1:
        candidates.append(("combined", [r for t in ui_tables for r in as_rows(t)], False))

    best = None
    for label, rows, enforce_size in candidates:
        used: set[int] = set()
        missing = []
        for er in required:
            idx = next((i for i, ar in enumerate(rows) if i not in used and _row_holds(ar, er, labels)), None)
            if idx is None:
                missing.append(er)
            else:
                used.add(idx)
        too_many = enforce_size and turn.match == "rows" and len(rows) > len(exp_rows)
        if not missing and not too_many:
            return checks.CheckResult("data_correct", True,
                                      f"table {label}: all {len(required)} expected row(s) found")
        score = (len(missing), too_many)
        if best is None or score < best[0]:
            best = (score, missing, len(rows), too_many)

    # Single-value answers may only be stated in the text (e.g. a percentage).
    if len(required) == 1:
        nums = _numbers_in_text(answer)
        wanted = [_cell(v) for v in required[0] if isinstance(_cell(v), float)]
        if wanted and all(any(_same(n, w) for n in nums) for w in wanted):
            return checks.CheckResult("data_correct", True, "expected value stated in the answer text")

    if best is None:
        return checks.CheckResult("data_correct", False, "no result table was shown")
    (_, missing, n_rows, too_many) = best
    detail = f"{len(missing)} of {len(required)} expected row(s) not found"
    if too_many:
        detail += f"; table has {n_rows} rows, expected {len(exp_rows)}"
    return checks.CheckResult("data_correct", False, detail, local_only={"missing": missing[:10]})


def check_tools_ok(trace: dict[str, Any], turn: Any) -> checks.CheckResult:
    errors = [tc["name"] for tc in trace.get("tool_calls", []) if tc.get("is_error")]
    rounds = len(trace.get("llm_calls", []))
    problems = []
    if turn.error:
        problems.append(f"chat error: {turn.error.get('error_title') or turn.error.get('error')}")
    if errors:
        problems.append(f"{len(errors)} tool error(s)")
    if any("too many tool calls" in w.lower() for w in turn.warnings):
        problems.append("stopped after too many tool calls")
    # Queries that failed inside a successful tool call (e.g. wrong SQL function) and were retried.
    failed_queries = 0
    for tc in trace.get("tool_calls", []):
        if tc["name"] != "query":
            continue
        try:
            data = json.loads(tc.get("raw_result") or "{}")
        except (TypeError, json.JSONDecodeError):
            continue
        items = data.get("results", [data]) if isinstance(data, dict) else []
        failed_queries += sum(1 for r in items if isinstance(r, dict) and r.get("ok") is False)
    note = f"; {failed_queries} failed query(ies), recovered" if failed_queries else ""
    return checks.CheckResult("tools_ok", not problems,
                              ("; ".join(problems) or f"{rounds} LLM call(s), no tool errors") + note,
                              local_only={"failed_queries": failed_queries})


def expected_text(turn: GoldenTurn, notes: str = "") -> str:
    parts = []
    exp = turn.expected or {}
    if exp.get("rows") is not None:
        rows = exp["rows"]
        shown = "\n".join(" | ".join("" if v is None else str(v) for v in r) for r in rows[:30])
        # Column names are left out: they are the real names the app hides (this text is uploaded).
        parts.append(f"Correct result from the data ({len(rows)} row(s)):\n{shown}")
    if turn.expected_answer:
        parts.append("Expected answer (the figures and insights a correct answer gives):\n"
                     + turn.expected_answer.strip())
    if turn.expected_behavior:
        parts.append("Required behaviour: " + " ".join(turn.expected_behavior.split()))
    if notes:
        parts.append(f"Meaning notes: {notes}")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Judge metrics (Opus)
# ---------------------------------------------------------------------------

def single_turn_metrics(judge):
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
    from deepeval.test_case import LLMTestCaseParams

    correctness = GEval(
        name="Correctness",
        model=judge,
        threshold=0.5,
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT, LLMTestCaseParams.EXPECTED_OUTPUT],
        evaluation_steps=[
            "The expected output is either the correct result from the data or a description of the required behaviour.",
            "The actual output is the assistant's written answer. A result table is shown to the user separately and is "
            "NOT included, so do not penalise the answer for not repeating every number of the table.",
            "If the expected output is data: penalise heavily any number, name, ranking or conclusion in the answer that "
            "contradicts it (numbers rounded or written in Indian format, e.g. 3,52,790, are fine).",
            "If the expected output is a behaviour: check the answer does what it requires and nothing it forbids.",
            "Penalise answers that misread the question (wrong period, wrong filter, wrong grouping) or invent meanings.",
        ],
    )
    return [
        correctness,
        AnswerRelevancyMetric(model=judge, threshold=0.5),
        FaithfulnessMetric(model=judge, threshold=0.5),
    ]


def conversation_metrics(judge):
    from deepeval.metrics import ConversationCompletenessMetric, KnowledgeRetentionMetric

    return [KnowledgeRetentionMetric(model=judge, threshold=0.5),
            ConversationCompletenessMetric(model=judge, threshold=0.5)]


def tool_context(trace: dict[str, Any]) -> list[str]:
    """What the model received from tools (masked), as the judge's evidence."""
    out = []
    for tc in trace.get("tool_calls", []):
        seen = tc.get("result_seen_by_model")
        if seen is None:
            continue
        text = seen if isinstance(seen, str) else json.dumps(seen, ensure_ascii=False)
        out.append(f"[{tc['name']}] {text}")
    return out or ["(no tool results)"]


# ---------------------------------------------------------------------------
# Cost of the app's own LLM calls
# ---------------------------------------------------------------------------

def chat_cost_usd(trace: dict[str, Any]) -> float:
    from conversation.constants import call_cost_usd

    total = 0.0
    for call in trace.get("llm_calls", []):
        u = (call.get("response") or {}).get("usage") or {}
        i, o = call_cost_usd(call.get("model"), int(u.get("input_tokens") or 0), int(u.get("output_tokens") or 0),
                             int(u.get("cache_creation_input_tokens") or 0), int(u.get("cache_read_input_tokens") or 0))
        total += i + o
    return total


def turn_wait_seconds() -> float:
    """Pause between turns of a conversation, like a person reading the answer (lets background
    summarisation run as it would for a real user)."""
    return float(os.getenv("EVAL_TURN_WAIT_SECONDS", "5"))


def sleep_between_turns() -> None:
    time.sleep(turn_wait_seconds())
