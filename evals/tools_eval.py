"""Tools evaluation: run golden tool cases against the MCP server (no LLM) and judge them by code."""
from __future__ import annotations

import datetime as dt
import decimal
import json
import math
import re
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Any

import yaml

from evals.checks import _find_name_leaks, real_names
from evals.config import Dataset

_PLACEHOLDER = re.compile(r"\{(t|c):([^}]+)\}")


# ---------------------------------------------------------------------------
# Golden cases
# ---------------------------------------------------------------------------

@dataclass
class Case:
    id: str
    area: str
    title: str
    expect: str
    spec: dict[str, Any]

    @property
    def user(self) -> str:
        return self.spec.get("user", "full_access")


def load_cases(ds: Dataset, filename: str = "tools.yaml") -> list[Case]:
    """The cases in the dataset's goldens/tools.yaml."""
    path = ds.goldens_dir / filename
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [Case(id=c["id"], area=c["area"], title=c["title"], expect=c["expect"], spec=c) for c in raw["cases"]]


# ---------------------------------------------------------------------------
# Names: real <-> token, from the app's catalog
# ---------------------------------------------------------------------------

class Names:
    def __init__(self, ds: Dataset):
        from catalog.models import SourceColumn, SourceObject

        objs = SourceObject.objects.filter(source__tenant__slug=ds.tenant_slug, source__source_id=ds.source_id)
        self.tables = {o.object_name: o.anonymized_name for o in objs}
        self.columns = {c.column_name: c.anonymized_name
                        for c in SourceColumn.objects.filter(source_object__in=objs)}
        self.source_id = ds.source_id
        self.all_real = real_names(ds.tenant_slug, ds.source_id)

    @staticmethod
    def other_source_table(text: str) -> str:
        """{o:<source_id>:<table>} -> that table's token in ANOTHER source (for isolation tests)."""
        from catalog.models import SourceObject

        def sub(m: re.Match) -> str:
            obj = SourceObject.objects.filter(source__source_id=m.group(1), object_name=m.group(2)).first()
            if not obj or not obj.anonymized_name:
                raise KeyError(f"Unknown table '{m.group(2)}' in source '{m.group(1)}'")
            return obj.anonymized_name
        return re.sub(r"\{o:([^:}]+):([^}]+)\}", sub, text)

    def render(self, text: str, mode: str) -> str:
        """mode 'token' -> what the model would write; 'real' -> real names for the reference DB."""
        text = self.other_source_table(text)
        def sub(m: re.Match) -> str:
            kind, name = m.group(1), m.group(2)
            mapping = self.tables if kind == "t" else self.columns
            if name not in mapping:
                raise KeyError(f"Unknown {'table' if kind == 't' else 'column'} '{name}' in golden case")
            return mapping[name] if mode == "token" else f'"{name}"' if kind == "c" else name
        return _PLACEHOLDER.sub(sub, text.replace("{source}", self.source_id))

    def render_obj(self, obj: Any, mode: str) -> Any:
        if isinstance(obj, str):
            return self.render(obj, mode)
        if isinstance(obj, dict):
            return {k: self.render_obj(v, mode) for k, v in obj.items()}
        if isinstance(obj, list):
            return [self.render_obj(v, mode) for v in obj]
        return obj


# ---------------------------------------------------------------------------
# Reference answers (DuckDB on the reference CSV)
# ---------------------------------------------------------------------------

class Reference:
    def __init__(self, ds: Dataset):
        import duckdb

        self.con = duckdb.connect()
        table = ds.source.get("reference_table") or Path(ds.reference_file).stem
        path = ds.reference_file.replace("'", "''")
        types = ds.source.get("reference_types") or {}
        opt = ""
        if types:
            pairs = ", ".join(f"'{k}': '{v}'" for k, v in types.items())
            opt = f", types = {{{pairs}}}"
        self.con.execute(f"CREATE VIEW \"{table}\" AS SELECT * FROM read_csv('{path}'{opt})")

    def run(self, sql: str) -> tuple[list[str], list[list[Any]]]:
        cur = self.con.execute(sql)
        return [d[0] for d in cur.description], [list(r) for r in cur.fetchall()]


# ---------------------------------------------------------------------------
# Comparison helpers
# ---------------------------------------------------------------------------

def _norm(v: Any) -> Any:
    if v is None:
        return None
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float, decimal.Decimal)):
        return float(v)
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    s = str(v).strip()
    try:
        return float(s)
    except ValueError:
        return s


def _close(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        return math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-6)
    return a == b


def _sort_key(row: list[Any]) -> tuple:
    return tuple((0, x) if isinstance(x, float) else (1, "") if x is None else (2, str(x)) for x in row)


def rows_equal(actual: list[list[Any]], expected: list[list[Any]]) -> tuple[bool, str]:
    a = sorted(([_norm(x) for x in r] for r in actual), key=_sort_key)
    e = sorted(([_norm(x) for x in r] for r in expected), key=_sort_key)
    if len(a) != len(e):
        return False, f"{len(a)} rows returned, expected {len(e)}"
    for i, (ra, re_) in enumerate(zip(a, e)):
        if len(ra) != len(re_) or not all(_close(x, y) for x, y in zip(ra, re_)):
            return False, f"row {i + 1} differs: got {ra}, expected {re_}"
    return True, f"{len(a)} row(s) match"


# ---------------------------------------------------------------------------
# Running one case
# ---------------------------------------------------------------------------

@dataclass
class CaseResult:
    id: str
    area: str
    title: str
    expect: str
    status: str                       # PASS / FAIL / SKIP / ERROR
    detail: str = ""
    sent: dict[str, Any] = field(default_factory=dict)       # what was sent (masked)
    reference_sql: str | None = None                          # real-name SQL (LOCAL ONLY)
    expected: Any = None
    received: Any = None
    local_only: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0


class ToolsRunner:
    def __init__(self, ds: Dataset, call: bool = True):
        self.ds = ds
        self.names = Names(ds)
        self.ref = Reference(ds)
        self.call = call
        self._callers: dict[str, Any] = {}

    def caller(self, label: str):
        if label not in self._callers:
            from evals.mcp_client import ToolCaller

            self._callers[label] = ToolCaller(self.ds, label)
        return self._callers[label]

    # -- request building --------------------------------------------------------------
    def build(self, case: Case) -> tuple[str, dict[str, Any], str | None]:
        """Returns (tool, arguments sent to MCP, reference SQL with real names or None)."""
        s = case.spec
        if "tool" in s:
            return s["tool"], self.names.render_obj(s.get("args") or {}, "token"), None
        if "raw_sql" in s:
            sql, ref = s["raw_sql"], None
        elif "raw_sql_template" in s:
            sql, ref = self.names.render(s["raw_sql_template"], "token"), None
        else:
            sql = self.names.render(s["sql"], "token")
            ref = self.names.render(s["sql"], "real")
        item: dict[str, Any] = {"source_id": s.get("query_source", self.ds.source_id), "sql": " ".join(sql.split())}
        if "limit" in s:
            item["limit"] = s["limit"]
        return "query", {"queries": [item]}, ref

    # -- evaluation --------------------------------------------------------------------
    def run_case(self, case: Case) -> CaseResult:
        res = CaseResult(case.id, case.area, case.title, case.expect, status="ERROR")
        if case.user not in self.ds.users:
            res.status, res.detail = "SKIP", f"test user '{case.user}' not configured in dataset.toml"
            return res
        try:
            tool, args, ref_sql = self.build(case)
        except KeyError as exc:
            res.detail = str(exc)
            return res
        res.sent = {"tool": tool, "arguments": args}
        res.reference_sql = ref_sql

        expected_rows = None
        if ref_sql and case.expect in ("match_reference", "kpi_matches"):
            cols, expected_rows = self.ref.run(" ".join(ref_sql.split()))
            res.expected = {"columns": cols, "rows": expected_rows}
        if not self.call:
            res.status, res.detail = "PREVIEW", ""
            return res

        r = self.caller(case.user).call(tool, args)
        res.duration_ms = r.duration_ms
        data = r.data if isinstance(r.data, dict) else {"_raw": r.raw}
        res.received = data
        check = getattr(self, f"_check_{case.expect}")
        passed, detail = check(case, r, data, expected_rows, res)
        res.status, res.detail = ("PASS" if passed else "FAIL"), detail
        return res

    def _leaks(self, obj: Any) -> list[str]:
        return sorted(_find_name_leaks(obj, self.names.all_real))

    def _check_match_reference(self, case, r, data, expected_rows, res):
        if not r.ok:
            return False, f"tool error: {data.get('error') or r.raw[:200]}"
        return rows_equal(data.get("rows") or [], expected_rows or [])

    def _check_rejected(self, case, r, data, expected_rows, res):
        if r.ok:
            return False, f"NOT refused: returned {data.get('row_count', '?')} row(s)"
        err = str(data.get("error") or r.raw)
        leaked = self._leaks({"error": err})
        res.local_only["error_text"] = err
        if leaked:
            res.local_only["leaked"] = leaked
            return False, f"refused, but the error shows {len(leaked)} real name(s)"
        return True, "refused: " + err[:120]

    def _check_masked(self, case, r, data, expected_rows, res):
        if not r.ok:
            return False, f"tool error: {data.get('error') or r.raw[:200]}"
        leaked = self._leaks(data)
        res.local_only["leaked"] = leaked
        return (not leaked), (f"{len(leaked)} real name(s) in output" if leaked else "no real names in output")

    def _check_masked_headers(self, case, r, data, expected_rows, res):
        if not r.ok:
            return False, f"tool error: {data.get('error') or r.raw[:200]}"
        cols = data.get("columns") or []
        leaked = sorted(c for c in cols if c in self.names.all_real)
        res.local_only["leaked"] = leaked
        return (not leaked), (f"real header(s): {len(leaked)}" if leaked else f"headers masked: {cols[:6]}")

    def _check_row_limit(self, case, r, data, expected_rows, res):
        if not r.ok:
            return False, f"tool error: {data.get('error') or r.raw[:200]}"
        n, want = len(data.get("rows") or []), case.spec["rows"]
        ok = n == want and bool(data.get("truncated"))
        return ok, f"{n} rows, truncated={data.get('truncated')} (expected {want}, truncated=True)"

    def _check_capped(self, case, r, data, expected_rows, res):
        if not r.ok:
            return False, f"tool error: {data.get('error') or r.raw[:200]}"
        n, asked = len(data.get("rows") or []), case.spec["limit"]
        ok = n < asked and bool(data.get("truncated"))
        return ok, f"{n} rows for limit {asked}, truncated={data.get('truncated')}, server max_rows={data.get('max_rows')}"

    def _check_kpi_matches(self, case, r, data, expected_rows, res):
        if not r.ok:
            return False, f"tool error: {data.get('error') or r.raw[:200]}"
        col = case.spec["kpi_column"]
        # Single-row results carry kpi_summary; multi-row results carry cross_row_kpi_summary.
        # The chat passes whichever is present to the model.
        summary = data.get("kpi_summary") or data.get("cross_row_kpi_summary") or {}
        kpi = summary.get(col)
        if not kpi:
            return False, f"no KPI summary for '{col}' (have {list(summary.keys())})"
        idx = res.expected["columns"].index(col)
        vals = [float(row[idx]) for row in expected_rows if row[idx] is not None]
        want = {"count": float(len(vals)), "min": min(vals), "max": max(vals), "sum": sum(vals)}
        bad = [k for k, v in want.items() if k in kpi and not _close(float(kpi[k]), v)]
        missing = [k for k in want if k not in kpi]
        if bad or missing:
            return False, f"mismatch {bad} missing {missing}: got {{{', '.join(f'{k}: {kpi.get(k)}' for k in want)}}}"
        return True, "count/min/max/sum match the data"

    def _check_not_listed(self, case, r, data, expected_rows, res):
        other = self.names.render(case.spec["other_source"], "token")
        if not r.ok:
            return True, f"refused ({str(data.get('error'))[:80]})"
        listed = [s.get("source_id") for s in data.get("sources") or [] if isinstance(s, dict)]
        return (other not in listed), (f"'{other}' IS listed" if other in listed else f"not listed (sees {listed})")
