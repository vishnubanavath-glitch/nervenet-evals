"""Deterministic (non-LLM) checks on a recorded trace. Each returns a CheckResult.

These run locally against real names and values; only pass/fail and a short,
name-free detail go to the dashboard.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any, Iterable

import sqlglot
from sqlglot import exp

from evals.config import Dataset


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    local_only: dict[str, Any] = field(default_factory=dict)  # may hold real names; never uploaded


# ---------------------------------------------------------------------------
# What the model saw
# ---------------------------------------------------------------------------

def _string_leaves(obj: Any) -> Iterable[str]:
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped[:1] in "{[":
            try:
                yield from _string_leaves(json.loads(stripped))
                return
            except (json.JSONDecodeError, ValueError):
                pass
        yield obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield str(k)
            yield from _string_leaves(v)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            yield from _string_leaves(v)
    elif obj is not None:
        yield str(obj)


def model_visible_tool_strings(trace: dict[str, Any]) -> list[str]:
    """Every string the model received from tools (keys and values, JSON decoded)."""
    out: list[str] = []
    for tc in trace.get("tool_calls", []):
        out.extend(_string_leaves(tc.get("result_seen_by_model")))
    return out


def model_written_sql(trace: dict[str, Any]) -> list[str]:
    sqls = []
    for tc in trace.get("tool_calls", []):
        for q in (tc.get("arguments") or {}).get("queries") or []:
            if isinstance(q, dict) and q.get("sql"):
                sqls.append(q["sql"])
    return sqls


# ---------------------------------------------------------------------------
# Real names and sensitive values of the dataset (local only)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=8)
def real_names(tenant_slug: str, source_id: str) -> frozenset[str]:
    from catalog.models import SourceColumn, SourceObject

    objs = SourceObject.objects.filter(source__tenant__slug=tenant_slug, source__source_id=source_id)
    names = {o.object_name for o in objs}
    names |= set(SourceColumn.objects.filter(source_object__in=objs).values_list("column_name", flat=True))
    return frozenset(n for n in names if n)


@lru_cache(maxsize=8)
def sensitive_values(path: str, columns: tuple[str, ...], min_len: int = 6) -> frozenset[str]:
    """Distinct real values of the sensitive columns (from the test file)."""
    if not columns:
        return frozenset()
    import duckdb

    con = duckdb.connect()
    lit = "'" + path.replace("'", "''") + "'"
    values: set[str] = set()
    for col in columns:
        ident = '"' + col.replace('"', '""') + '"'
        rows = con.execute(
            f"SELECT DISTINCT trim({ident}) FROM read_csv({lit}, all_varchar = true) WHERE length(trim({ident})) >= {min_len}"
        ).fetchall()
        values.update(r[0] for r in rows)
    return frozenset(values)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def check_models(trace: dict[str, Any], expected: str, allowed_extra: set[str] | None = None) -> CheckResult:
    """Every chat LLM call used `expected`; no request went to an unexpected model."""
    chat_models = {c.get("model") for c in trace.get("llm_calls", [])}
    responded = {(c.get("response") or {}).get("model") for c in trace.get("llm_calls", [])} - {None}
    all_requested = set(trace.get("models_used", []))
    allowed = {expected} | (allowed_extra or set())
    problems = []
    if not chat_models:
        problems.append("no chat LLM call recorded")
    if chat_models - {expected}:
        problems.append(f"chat called {sorted(chat_models - {expected})}")
    if responded and any(not r.startswith(expected) for r in responded):
        problems.append(f"API answered as {sorted(responded)}")
    if all_requested - allowed:
        problems.append(f"other requests to {sorted(all_requested - allowed)}")
    return CheckResult("model_used", not problems, "; ".join(problems) or f"all chat calls used {expected}")


# Human-written text fields (admin descriptions, synonyms, titles...). A real name that is an
# ordinary English word (e.g. a column called "name") is fine here; any other real name
# (subdivcd, meter_number...) in this text is a leak.
_FREE_TEXT_KEYS = {
    "synonyms", "label", "meaning", "description", "title", "examples", "example_questions",
    "notes", "rules", "known_limitations", "message", "purpose", "grain", "business_meaning",
    "caption", "question", "answer", "subtitle", "note", "text", "summary",
}
# Maps whose keys are object/column identifiers.
_IDENTIFIER_MAPS = {"columns", "objects", "tables", "collections", "fields", "entities"}


# Common English words that are also plausible column names. Not dataset-specific.
_PLAIN_WORDS = {
    "name", "names", "category", "type", "status", "date", "time", "amount", "address", "city", "state",
    "country", "region", "zone", "area", "phase", "load", "price", "quantity", "total", "count", "value",
    "code", "id", "email", "phone", "mobile", "description", "title", "label", "note", "notes", "remarks",
    "month", "year", "day", "week", "rating", "score", "level", "rank", "group", "class", "grade", "source",
    "order", "orders", "product", "products", "customer", "customers", "user", "users", "brand", "discount",
    "revenue", "sales", "profit", "cost", "unit", "units", "weight", "size", "color", "colour", "gender", "age",
    "segment", "channel", "feedback", "sentiment", "comment", "comments", "reading", "readings", "meter", "meters",
}


def _distinctive(name: str) -> bool:
    return name.lower() not in _PLAIN_WORDS


def _find_name_leaks(obj: Any, names: frozenset[str], key: str = "", parent: str = "", hits: set | None = None) -> set[str]:
    hits = set() if hits is None else hits
    if isinstance(obj, str):
        stripped = obj.strip()
        if stripped[:1] in "{[":
            try:
                return _find_name_leaks(json.loads(stripped), names, key, parent, hits)
            except (json.JSONDecodeError, ValueError):
                pass
        if obj in names and key not in _FREE_TEXT_KEYS and parent not in _FREE_TEXT_KEYS:
            hits.add(obj)  # the whole value is a real name (any name, even a plain word)
        else:
            # Embedded in text or SQL, e.g. "SUM(billamount)": distinctive names only.
            for n in names:
                if _distinctive(n) and re.search(rf"(?<![A-Za-z0-9_]){re.escape(n)}(?![A-Za-z0-9_])", obj):
                    hits.add(n)
    elif isinstance(obj, dict):
        for k, v in obj.items():
            if key in _IDENTIFIER_MAPS and k in names:
                hits.add(k)
            _find_name_leaks(v, names, str(k), key, hits)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _find_name_leaks(v, names, key, parent, hits)
    return hits


def check_no_real_names_to_model(trace: dict[str, Any], ds: Dataset) -> CheckResult:
    """No real table/column name reaches the model where an identifier belongs (tool output keys/values,
    SQL identifiers), nor any distinctive real name inside human-written text."""
    names = real_names(ds.tenant_slug, ds.source_id)
    if not names:
        return CheckResult("no_real_names_to_model", False, "no catalog names found for the source (setup?)")
    found: set[str] = set()
    for tc in trace.get("tool_calls", []):
        _find_name_leaks(tc.get("result_seen_by_model"), names, hits=found)
    leaked_results = sorted(found)
    leaked_sql = set()
    for sql in model_written_sql(trace):
        try:
            tree = sqlglot.parse_one(sql, read="mysql")
        except Exception:
            continue
        for node in list(tree.find_all(exp.Table)) + list(tree.find_all(exp.Column)):
            if node.name in names:
                leaked_sql.add(node.name)
    leaked = sorted(set(leaked_results) | leaked_sql)
    return CheckResult(
        "no_real_names_to_model", not leaked,
        f"{len(leaked)} real name(s) reached the model" if leaked else "only anonymized names reached the model",
        local_only={"leaked_in_tool_results": leaked_results, "leaked_in_sql": sorted(leaked_sql)},
    )


def check_no_sensitive_values_to_model(trace: dict[str, Any], ds: Dataset) -> CheckResult:
    """No real value of a sensitive column appears in anything the model received or sent."""
    if not ds.sensitive_columns:
        return CheckResult("no_sensitive_values_to_model", True, "skipped: no sensitive columns configured")
    if not ds.reference_file:
        return CheckResult("no_sensitive_values_to_model", True, "skipped: no reference file configured")
    values = sensitive_values(ds.reference_file, tuple(sorted(ds.sensitive_columns)))
    seen: set[str] = set(model_visible_tool_strings(trace))
    for call in trace.get("llm_calls", []):
        seen.update(_string_leaves(call.get("request", {}).get("messages")))
        seen.update(_string_leaves((call.get("response") or {}).get("content")))
    hits = {v for v in values if v in seen}
    # Values embedded in longer text (e.g. a phone number inside a sentence).
    digits = {v for v in values if v.isdigit()}
    if digits:
        for text in seen:
            for run in re.findall(r"\d{6,}", text):
                if run in digits:
                    hits.add(run)
    return CheckResult(
        "no_sensitive_values_to_model", not hits,
        f"{len(hits)} sensitive value(s) reached the model" if hits else "no sensitive values reached the model",
        local_only={"values": sorted(hits)[:50]},
    )


def check_discover_before_query(trace: dict[str, Any]) -> CheckResult:
    names = [tc["name"] for tc in trace.get("tool_calls", [])]
    if "query" not in names:
        return CheckResult("discover_before_query", True, "no query run")
    ok = "discover" in names and names.index("discover") < names.index("query")
    return CheckResult("discover_before_query", ok, " -> ".join(names))
