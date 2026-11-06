"""Facts about the reference data (per column and per table), used as the answer key in the schema-context evaluation."""
from __future__ import annotations

import re
from typing import Any

from evals.tools_eval import Reference

_DATE_PATTERNS = {
    "dd-mm-yyyy": r"^\d{2}-\d{2}-\d{4}",
    "yyyy-mm-dd": r"^\d{4}-\d{2}-\d{2}",
    "dd/mm/yyyy": r"^\d{2}/\d{2}/\d{4}",
}


def _q(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def profile_table(ref: Reference, table: str, top: int = 8) -> dict[str, Any]:
    con = ref.con
    t = _q(table)
    rows = con.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
    described = con.execute(f"DESCRIBE SELECT * FROM {t}").fetchall()
    columns: dict[str, Any] = {}
    for name, dtype, *_ in described:
        c = _q(name)
        nulls, distinct = con.execute(f"SELECT COUNT(*) - COUNT({c}), COUNT(DISTINCT {c}) FROM {t}").fetchone()
        top_vals = con.execute(
            f"SELECT {c}, COUNT(*) n FROM {t} WHERE {c} IS NOT NULL GROUP BY 1 ORDER BY n DESC, 1 LIMIT {top}"
        ).fetchall()
        info: dict[str, Any] = {
            "type": dtype, "rows": rows, "nulls": nulls, "null_pct": round(100 * nulls / rows, 2) if rows else 0,
            "distinct": distinct,
            "top_values": [[_plain(v), n] for v, n in top_vals],
        }
        if any(k in dtype for k in ("INT", "DOUBLE", "DECIMAL", "FLOAT", "HUGEINT")):
            mn, mx, avg, neg, zero = con.execute(
                f"SELECT MIN({c}), MAX({c}), AVG({c}), SUM(CASE WHEN {c} < 0 THEN 1 ELSE 0 END), "
                f"SUM(CASE WHEN {c} = 0 THEN 1 ELSE 0 END) FROM {t}").fetchone()
            info.update(min=_plain(mn), max=_plain(mx), avg=round(avg, 4) if avg is not None else None,
                        negatives=int(neg or 0), zeros=int(zero or 0))
            fractional = con.execute(
                f"SELECT COUNT(*) FROM {t} WHERE {c} IS NOT NULL AND {c} <> FLOOR({c})").fetchone()[0]
            info["has_fractions"] = bool(fractional)
        elif "VARCHAR" in dtype:
            lmin, lmax = con.execute(f"SELECT MIN(LENGTH({c})), MAX(LENGTH({c})) FROM {t}").fetchone()
            info.update(min_length=lmin, max_length=lmax)
            sample = [str(v) for v, _ in top_vals[:5]]
            for label, pat in _DATE_PATTERNS.items():
                if sample and all(re.match(pat, s) for s in sample):
                    info["looks_like_date"] = label
            if distinct and distinct <= 20:
                cases = con.execute(
                    f"SELECT COUNT(DISTINCT {c}) - COUNT(DISTINCT LOWER({c})) FROM {t}").fetchone()[0]
                if cases:
                    info["case_variants"] = int(cases)
        info["unique_per_row"] = distinct == rows - nulls and rows > 0
        columns[name] = info
    return {"table": table, "rows": rows, "columns": columns}


def _plain(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


def column_facts_text(name: str, info: dict[str, Any]) -> str:
    """Readable facts for the judge."""
    parts = [f"Column `{name}`: stored as {info['type']}",
             f"{info['rows']:,} rows, {info['nulls']:,} empty ({info['null_pct']}%), {info['distinct']:,} distinct values"]
    if "min" in info:
        parts.append(f"min {info['min']}, max {info['max']}, average {info['avg']}, "
                     f"{info['negatives']:,} negative, {info['zeros']:,} zero, "
                     f"{'has' if info.get('has_fractions') else 'no'} fractional values")
    if "min_length" in info:
        parts.append(f"text length {info['min_length']}-{info['max_length']}")
    if info.get("looks_like_date"):
        parts.append(f"values look like dates in {info['looks_like_date']} format (stored as text)")
    if info.get("case_variants"):
        parts.append(f"{info['case_variants']} value(s) differ only by letter case")
    if info.get("unique_per_row"):
        parts.append("every non-empty value is unique")
    tops = ", ".join(f"{v!r} ({n:,})" for v, n in info["top_values"])
    parts.append(f"most common values: {tops}")
    return "; ".join(parts) + "."
