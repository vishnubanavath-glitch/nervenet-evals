"""Schema-context evaluation: the generator (Haiku), graded against facts from the reference data.

Nothing is saved in the app: the blank draft is built in memory with the app's own
draft function, and the generator returns suggestions without writing them
(it only adds an audit event, as it does for any user).
"""
from __future__ import annotations

import asyncio
import copy
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.checks import CheckResult
from evals.config import EVALS_DIR, Dataset
from evals.profile import column_facts_text
from evals.tracing import Recorder

VALIDATOR = EVALS_DIR.parent / ".claude" / "skills" / "schema-context-generator" / "scripts" / "validate_context.py"
PERSONAL_OK = {"pii", "confidential", "secret"}


# ---------------------------------------------------------------------------
# Running the generator (read-only)
# ---------------------------------------------------------------------------

def blank_draft(source, tables: list[str]) -> dict[str, Any]:
    """The draft the app would create at step 4, built in memory (not saved), with no enrichment."""
    from catalog.onboarding import _profile_data_source, draft_schema_context_payload

    profile = _profile_data_source(source, tables)
    return draft_schema_context_payload(source, profile, enrichment=None)


@dataclass
class GeneratorRun:
    step: str
    output: dict[str, Any] | None = None
    error: str | None = None
    trace: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0


def run_step3(source_object, user) -> GeneratorRun:
    import time

    from conversation.schema_context_generator import suggest_table_settings

    run = GeneratorRun("column_settings")
    started = time.time()
    with Recorder() as trace:
        try:
            run.output = suggest_table_settings(source_object, user)
        except Exception as exc:  # SuggestionError and anything else
            run.error = f"{type(exc).__name__}: {exc}"
    run.trace, run.duration_ms = trace.to_dict(), int((time.time() - started) * 1000)
    return run


def run_step5(source, draft: dict[str, Any], user) -> GeneratorRun:
    import time

    from catalog.models import SchemaContext
    from conversation.schema_context_generator import suggest_schema_context

    run = GeneratorRun("schema_context")
    unsaved = SchemaContext(source=source, status=SchemaContext.STATUS_DRAFT, context=copy.deepcopy(draft))
    started = time.time()
    with Recorder() as trace:
        try:
            run.output = suggest_schema_context(unsaved, user)
        except Exception as exc:
            run.error = f"{type(exc).__name__}: {exc}"
    run.trace, run.duration_ms = trace.to_dict(), int((time.time() - started) * 1000)
    return run


def column_suggestions(run: GeneratorRun, table: str) -> dict[str, dict[str, Any]]:
    """{column: {label, meaning, synonyms, role, sensitivity}} from either step's output."""
    if not run.output:
        return {}
    if run.step == "column_settings":
        return {c: {"label": s.get("display_label"), "meaning": s.get("meaning"), "synonyms": s.get("synonyms"),
                    "role": s.get("role_hint"), "sensitivity": s.get("sensitivity_classification")}
                for c, s in (run.output.get("columns") or {}).items()}
    cols = ((run.output.get("context") or {}).get("objects") or {}).get(table, {}).get("columns") or {}
    return {c: {"label": s.get("label"), "meaning": s.get("meaning"), "synonyms": s.get("synonyms"),
                "role": s.get("role"), "sensitivity": s.get("sensitivity_classification")}
            for c, s in cols.items() if isinstance(s, dict)}


# ---------------------------------------------------------------------------
# Code checks
# ---------------------------------------------------------------------------

def check_model(run: GeneratorRun, expected: str) -> CheckResult:
    used = set(run.trace.get("models_used") or [])
    settings = {c.get("model_setting") for c in run.trace.get("schema_calls", [])}
    ok = used == {expected} and settings <= {expected}
    return CheckResult("model_used", ok, f"requests went to {sorted(used) or 'nothing'}")


def check_coverage(sugg: dict[str, dict[str, Any]], columns: list[str]) -> CheckResult:
    missing = [c for c in columns if c not in sugg]
    thin = [c for c in columns if c in sugg and (not sugg[c].get("label") or not sugg[c].get("meaning"))]
    unknown_role = [c for c in columns if c in sugg and sugg[c].get("role") in (None, "", "unknown")]
    unknown_sens = [c for c in columns if c in sugg and sugg[c].get("sensitivity") in (None, "", "unknown")]
    problems = []
    if missing:
        problems.append(f"{len(missing)} column(s) not described")
    if thin:
        problems.append(f"{len(thin)} without label/meaning")
    # "unknown" role/sensitivity is an honest answer when the sample rows give nothing to go on
    # (e.g. a column that is empty in every sampled row), so it is reported, not failed.
    info = []
    if unknown_role:
        info.append(f"role left unknown: {', '.join(unknown_role)}")
    if unknown_sens:
        info.append(f"sensitivity left unknown: {', '.join(unknown_sens)}")
    detail = "; ".join(problems) or f"all {len(columns)} columns described"
    if info:
        detail += " (" + "; ".join(info) + ")"
    return CheckResult("coverage", not problems, detail,
                       local_only={"missing": missing, "thin": thin, "unknown_role": unknown_role,
                                   "unknown_sensitivity": unknown_sens})


def check_personal(sugg: dict[str, dict[str, Any]], personal: list[str]) -> CheckResult:
    if not personal:
        return CheckResult("personal_data_flagged", True, "no personal columns configured")
    not_flagged = {c: sugg.get(c, {}).get("sensitivity") for c in personal if sugg.get(c, {}).get("sensitivity") not in PERSONAL_OK}
    return CheckResult("personal_data_flagged", not not_flagged,
                       f"{len(personal) - len(not_flagged)}/{len(personal)} personal columns flagged"
                       + (f"; not flagged: {not_flagged}" if not_flagged else ""))


def check_dates(sugg: dict[str, dict[str, Any]], profile: dict[str, Any]) -> CheckResult:
    date_cols = [c for c, i in profile["columns"].items() if i.get("looks_like_date") or "DATE" in i["type"]]
    wrong = {c: sugg.get(c, {}).get("role") for c in date_cols if sugg.get(c, {}).get("role") != "date_time"}
    return CheckResult("dates_marked", not wrong,
                       f"{len(date_cols) - len(wrong)}/{len(date_cols)} date columns marked date_time"
                       + (f"; wrong: {wrong}" if wrong else ""))


def _norm_code(v: Any) -> str:
    s = str(v).strip().lower()
    return re.sub(r"\.0+$", "", s)


def check_code_mappings(mappings: dict[str, dict[str, str]], profile: dict[str, Any], ref, table: str) -> CheckResult:
    """Every mapped code must exist in the data (case-insensitive; 1.0 == 1)."""
    if not mappings:
        return CheckResult("code_mappings_real", True,
                           "no code mappings generated (the generator only sees a few sample rows)")
    bad: dict[str, list[str]] = {}
    total = 0
    for key, codes in mappings.items():
        _, _, col = key.partition(".")
        if col not in profile["columns"]:
            bad[key] = ["(unknown column)"]
            continue
        actual = {_norm_code(v[0]) for v in ref.con.execute(
            f'SELECT DISTINCT "{col}" FROM "{table}" WHERE "{col}" IS NOT NULL LIMIT 500').fetchall()}
        for code in codes:
            total += 1
            if _norm_code(code) not in actual:
                bad.setdefault(key, []).append(code)
    n_bad = sum(len(v) for v in bad.values())
    return CheckResult("code_mappings_real", not bad,
                       f"{total - n_bad}/{total} mapped codes exist in the data"
                       + (f"; not in data: {bad}" if bad else ""))


def check_sql_runs(output: dict[str, Any], ds: Dataset, table: str) -> CheckResult:
    """Every generated metric and query pattern runs on the real source (via the MCP query tool)."""
    from catalog.anonymization import tokenize_sql

    from evals.mcp_client import ToolCaller

    ctx = output.get("context") or {}
    items = [(f"metric {k}", m.get("sql")) for k, m in (ctx.get("metrics") or {}).items()]
    items += [(f"pattern {p.get('name')}", p.get("sql")) for p in (ctx.get("approved_query_patterns") or [])]
    items = [(n, s) for n, s in items if s]
    if not items:
        return CheckResult("generated_sql_runs", False, "no metrics or query patterns generated")
    caller = ToolCaller(ds, "full_access")
    failures = {}
    for name, sql in items:
        text = sql.strip().rstrip(";")
        if not re.match(r"(?is)^\s*(select|with)\b", text):
            text = f"SELECT {text} AS value FROM {table}"
        r = caller.sql(tokenize_sql(ds.source_id, text), limit=5)
        if not r.ok:
            failures[name] = str((r.data or {}).get("error") or r.raw)[:160]
    return CheckResult("generated_sql_runs", not failures,
                       f"{len(items) - len(failures)}/{len(items)} metrics/patterns run on the source",
                       local_only={"failures": failures, "sql": dict(items)})


def check_validator(draft: dict[str, Any], output: dict[str, Any], workdir: Path) -> CheckResult:
    if not VALIDATOR.exists():
        return CheckResult("validate_context", True, "validator not found; skipped")
    d, f = workdir / "draft.json", workdir / "generated_context.json"
    d.write_text(json.dumps(draft, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    f.write_text(json.dumps(output.get("context") or {}, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    p = subprocess.run([sys.executable, "-I", str(VALIDATOR), str(d), str(f)], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=VALIDATOR.parent)
    text = (p.stdout + p.stderr).strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return CheckResult("validate_context", p.returncode == 0,
                       ("passes" if p.returncode == 0 else "errors") + (f": {lines[0][:150]}" if lines else ""),
                       local_only={"output": text})


# ---------------------------------------------------------------------------
# Judge (Opus)
# ---------------------------------------------------------------------------

def column_metric(judge):
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCaseParams

    return GEval(
        name="Column description accuracy", model=judge, threshold=0.5, async_mode=True,
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        evaluation_steps=[
            "The input gives measured facts about one database column (type, empty values, distinct values, ranges, "
            "most common values). The actual output is an AI-written description of that column.",
            "Penalise any statement in the label or meaning that the facts contradict (e.g. 'never negative' when "
            "negatives exist, 'stored as a date' when it is text, wrong units implied by the values, wrong format).",
            "Check the role: identifier = identifies an entity/record (high distinct count); dimension = a category or "
            "code to group by; metric = a quantity worth summing/averaging; date_time = a date or time.",
            "Check sensitivity: personal data (person names, addresses, phone numbers) should be pii or confidential; "
            "business codes and amounts should not be pii.",
            "When the meaning of codes cannot be known from the data, the description should present it as uncertain "
            "rather than as fact. Do not penalise reasonable domain knowledge clearly implied by the name and values.",
            "Synonyms should be words a business user might really use for this column.",
        ],
    )


def table_metric(judge):
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCaseParams

    return GEval(
        name="Table context accuracy", model=judge, threshold=0.5, async_mode=True,
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        evaluation_steps=[
            "The input gives measured facts about a table and its columns; the actual output is AI-written table-level "
            "context: business domain, purpose, grain, default date column, known limitations, metrics (SQL), query "
            "patterns, code mappings and example questions.",
            "Check the grain against the facts (e.g. whether identifiers repeat across rows) and the default date column.",
            "Check known limitations are real (supported by the facts) and important ones are not missing.",
            "Check metrics and patterns aggregate sensibly: no summing of codes, identifiers or cumulative meter registers.",
            "Check code mappings use codes present in the data and do not state guessed meanings as fact.",
            "Check example questions can be answered from these columns.",
        ],
    )


_LOOP: asyncio.AbstractEventLoop | None = None


def _run(coro):
    """One event loop for all judging: the judge's HTTP client is tied to the loop it first ran on,
    so a fresh asyncio.run() per batch leaves it closing on a dead loop (noisy tracebacks at exit)."""
    global _LOOP
    if _LOOP is None or _LOOP.is_closed():
        _LOOP = asyncio.new_event_loop()
    return _LOOP.run_until_complete(coro)


async def _measure_all(pairs, concurrency: int = 6):
    sem = asyncio.Semaphore(concurrency)

    async def one(metric, tc):
        async with sem:
            try:
                await metric.a_measure(tc, _show_indicator=False)
            except Exception as exc:  # keep going; record the error
                metric.error = f"{type(exc).__name__}: {exc}"
            return metric
    return await asyncio.gather(*(one(m, tc) for m, tc in pairs))


def judge_columns(judge, sugg: dict[str, dict[str, Any]], profile: dict[str, Any], table_label: str):
    from deepeval.test_case import LLMTestCase

    pairs = []
    for col, info in profile["columns"].items():
        s = sugg.get(col)
        if not s:
            continue
        tc = LLMTestCase(input=f"Table: {table_label}. " + column_facts_text(col, info),
                         actual_output=json.dumps(s, ensure_ascii=False))
        pairs.append((column_metric(judge), tc))
    metrics = _run(_measure_all(pairs))
    return {col: m for (col, _), m in zip([(c, None) for c in profile["columns"] if c in sugg], metrics)}


def judge_table(judge, output: dict[str, Any], profile: dict[str, Any], table: str):
    from deepeval.test_case import LLMTestCase

    ctx = output.get("context") or {}
    obj = (ctx.get("objects") or {}).get(table, {})
    produced = {k: ctx.get(k) for k in ("business_domain", "known_limitations", "metrics",
                                         "approved_query_patterns", "code_mappings", "example_questions")}
    produced.update({k: obj.get(k) for k in ("business_name", "purpose", "grain", "default_date_column")})
    facts = [f"Table `{table}`: {profile['rows']:,} rows."]
    facts += [column_facts_text(c, i) for c, i in profile["columns"].items()]
    m = table_metric(judge)
    _run(_measure_all([(m, LLMTestCase(input="\n".join(facts),
                                               actual_output=json.dumps(produced, ensure_ascii=False)))]))
    return m
