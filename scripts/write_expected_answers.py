"""Write (or refresh) the `expected_answer` of every chat turn that has an expected_sql.

    uv run python scripts/write_expected_answers.py --dataset tgspdcl_meter_readings

The answer is generated from the reference CSV (the same answer key the data check uses):
the result as a numbered list in Indian number format, plus the key insights (highest,
lowest, total). Run it again whenever the reference data changes. Only the
expected_answer blocks in goldens/chat.yaml are rewritten; everything else is kept as written.
"""
from __future__ import annotations

import argparse
import datetime as dt
import decimal
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals.chat_eval import load_cases  # noqa: E402
from evals.config import load_dataset  # noqa: E402
from evals.tools_eval import Reference  # noqa: E402

MAX_ROWS = 15
MONEY = re.compile(r"bill|amount|payable|arrear|₹|rupee|revenue|crore|lakh", re.I)
PCT = re.compile(r"percent|share|%|ratio of", re.I)
COUNTISH = re.compile(r"how many|count|number of", re.I)


# ---------------------------------------------------------------------------
# Number formatting (Indian digit grouping)
# ---------------------------------------------------------------------------

def indian(n: float, decimals: int = 0) -> str:
    neg = n < 0
    s = f"{abs(n):.{decimals}f}"
    whole, _, frac = s.partition(".")
    if len(whole) > 3:
        head, tail = whole[:-3], whole[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        whole = ",".join(groups + [tail])
    return ("-" if neg else "") + whole + (f".{frac}" if frac else "")


import sqlglot  # noqa: E402
from sqlglot import exp  # noqa: E402

MONEY_COL = re.compile(r"billamount|totalamount|arrears", re.I)
UNITS_COL = re.compile(r"netbilled_units|presentreading|previous_kwh_reading|spotprsmrdg|consumption", re.I)
FRIENDLY = {
    "billamount": "bill amount", "totalamount": "total payable", "netbilled_units": "net billed units",
    "old_arrears": "old arrears", "new_arrears": "new arrears", "contracted_load": "contracted load (kW)",
    "spotbilldays": "billing days", "spotmd": "spot maximum demand", "billdemand": "bill demand",
    "service_number": "service connections", "meter_number": "meter numbers", "secname": "section",
    "subdivname": "sub-division", "tariffcat": "tariff category", "presentreading": "present reading",
    "previous_kwh_reading": "previous reading", "spotavg": "spot average units",
}


CONDITIONS = [
    (r"billflag\s*=\s*'S'", "billed"), (r"billflag\s*=\s*'U'", "unbilled"),
    (r"LOWER\(gj_flag\)\s*=\s*'yes'", "Gruha Jyothi"), (r"meter_phase\s*=\s*3", "three-phase"),
    (r"SUBSTRING\(readingdate,\s*4,\s*7\)\s*=\s*'04-2026'", "April 2026"),
    (r"SUBSTRING\(readingdate,\s*4,\s*7\)\s*=\s*'06-2026'", "June 2026"),
]


def words(sql_text: str) -> str:
    t = sql_text.replace('"', "")
    for pat, rep in CONDITIONS:
        t = re.sub(pat, rep, t, flags=re.I)
    t = re.sub(r"\b[a-z]\.(\w+)", r"\1", t)  # drop short table aliases (a.u -> u)
    for k, v in FRIENDLY.items():
        t = re.sub(rf"\b{k}\b", v, t)
    return t


# ---------------------------------------------------------------------------
# What each result column is (from the SQL), so it gets the right label and unit
# ---------------------------------------------------------------------------

def _inner(e: exp.Expression) -> exp.Expression:
    while isinstance(e, (exp.Alias, exp.Round, exp.Paren, exp.Cast)):
        e = e.this
    return e


def _cond_text(case: exp.Case) -> str:
    ifs = case.args.get("ifs") or []
    return words(ifs[0].this.sql()) if ifs else "condition"


def describe(e: exp.Expression) -> tuple[str, str, bool]:
    """(label, kind, summable) for one SELECT expression. kind: count | pct | money | units | number."""
    alias = e.alias if isinstance(e, exp.Alias) else ""
    sql = e.sql().lower()
    inner = _inner(e)
    pct = "100.0 *" in sql or "100 *" in sql
    if isinstance(inner, exp.Window):
        fn = inner.this
        if isinstance(fn, (exp.RowNumber,)) or fn.sql().upper().startswith(("RANK", "DENSE_RANK")):
            return "rank", "number", False
        if pct:
            return (alias or "share"), "pct", False
        kind = "money" if MONEY_COL.search(sql) else "units" if UNITS_COL.search(sql) else "count"
        return (alias if alias and len(alias) > 2 else "running total"), kind, False
    quant = inner if isinstance(inner, (exp.PercentileCont, exp.Quantile)) else None
    if quant is not None or re.match(r"(?i)^(percentile_cont|quantile_cont)\(", inner.sql()):
        m = re.search(r",\s*([0-9.]+)\)", inner.sql()) or re.search(r"\(([0-9.]+)\)", inner.sql())
        q = float(m.group(1)) if m else None
        col = re.search(r"\(([^,()]+)", inner.sql())
        what = words(col.group(1)) if col else "value"
        kind = "money" if MONEY_COL.search(sql) else "units" if UNITS_COL.search(sql) else "number"
        nth = f"{int(round(q * 100))}th percentile of " if q is not None else "percentile of "
        return (nth + what), kind, False
    if isinstance(inner, exp.Count):
        arg = inner.this
        if isinstance(arg, exp.Distinct):
            return f"distinct {words(arg.expressions[0].sql())}", "count", False
        return "count", "count", True
    if isinstance(inner, exp.Sum) and isinstance(_inner(inner.this), exp.Case):
        case = _inner(inner.this)
        ifs = case.args.get("ifs") or []
        then = ifs[0].args.get("true").sql() if ifs else ""
        if then in ("1", "1.0"):
            cond = _cond_text(case)
            return (cond if cond in ("billed", "unbilled", "Gruha Jyothi", "three-phase") else f"count where {cond}"), \
                "count", True
        if then and not pct:
            kind = "money" if MONEY_COL.search(then) else "units" if UNITS_COL.search(then) else "number"
            cond = _cond_text(case)
            joiner = " in " if cond in ("April 2026", "June 2026") else " where "
            return f"total {words(then)}{joiner}{cond}", kind, True
    if isinstance(inner, exp.Div) and not pct:
        def base(x):
            x = _inner(x)
            if isinstance(x, exp.Nullif):
                x = _inner(x.this)
            return words(x.this.sql()) if isinstance(x, exp.AggFunc) else words(x.sql())
        left = inner.this.sql()
        kind = "money" if MONEY_COL.search(left) else "units" if UNITS_COL.search(left) else "number"
        return (alias if alias and len(alias) > 2 else f"{base(inner.this)} per {base(inner.expression)}"), kind, False
    if pct:
        return (alias or "percentage"), "pct", False
    col_sql = sql
    kind = "money" if MONEY_COL.search(col_sql) else "units" if UNITS_COL.search(col_sql) else "number"
    for cls, word, summable in ((exp.Sum, "total", True), (exp.Avg, "average", False), (exp.Min, "lowest", False),
                                (exp.Max, "highest", False), (exp.Median, "median", False)):
        if isinstance(inner, cls):
            return f"{word} {words(inner.this.sql())}", kind, summable
    if isinstance(inner, exp.Column):
        return words(inner.name), kind, False
    if alias and len(alias) > 2:
        nice = alias.replace('_', ' ')
        if kind == 'number' and re.search(r'bill|amount|payable|arrear', nice):
            kind = 'money'
        elif kind == 'number' and re.search(r'units|consumption', nice):
            kind = 'units'
        return nice, kind, isinstance(inner, exp.Sum)
    return words(inner.sql())[:60], kind, isinstance(inner, exp.Sum)


def describe_named(e: exp.Expression) -> tuple[str, str, bool]:
    """describe(), but a descriptive alias from the SQL (e.g. april_units) names the column."""
    label, kind, summable = describe(e)
    alias = e.alias if isinstance(e, exp.Alias) else ""
    if len(alias) > 3 and label not in ("rank",):
        label = alias.replace("_", " ")
    return label, kind, summable


def select_columns(sql: str) -> list[tuple[str, str, bool]]:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return []
    while isinstance(tree, (exp.Union,)):
        tree = tree.this
    sel = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    return [describe_named(e) for e in sel.expressions] if sel else []


def key_columns(sql: str) -> list[bool]:
    """True for SELECT columns that name a row (plain columns / expressions with no aggregate or window)."""
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return []
    while isinstance(tree, exp.Union):
        tree = tree.this
    sel = tree if isinstance(tree, exp.Select) else tree.find(exp.Select)
    if not sel or not sel.args.get("group"):
        return []
    return [not (e.find(exp.AggFunc) or e.find(exp.Window)) for e in sel.expressions]


def question_labels(question: str, n: int) -> list[str] | None:
    """Measure names listed in the question ('...: readings, billed percentage and total bill')."""
    q = " ".join(question.split())
    if ":" not in q:
        return None
    part = q.split(":", 1)[1]
    items = [re.sub(r"^(the|and)\s+", "", x.strip(" .?")) for x in re.split(r",\s*|\s+and\s+", part) if x.strip(" .?")]
    if len(items) != n or any(len(x.split()) > 6 for x in items):
        return None  # only short measure names make good labels
    return items


def fmt(v, kind: str) -> str:
    if v is None:
        return "(empty)"
    if isinstance(v, (dt.date, dt.datetime)):
        return v.isoformat()
    if isinstance(v, decimal.Decimal):
        v = float(v)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return str(v).strip()
    whole = float(v).is_integer()
    if kind == "money":
        return ("-₹" if v < 0 else "₹") + indian(abs(v), 2)
    if kind == "pct":
        return indian(v, 2) + "%"
    if kind == "units":
        return indian(v, 0 if whole else 2) + " units"
    if kind == "count" or whole:
        return indian(v, 0)
    return indian(v, 2) if abs(v) >= 0.01 else f"{v:.4f}"


def answer_for(question: str, cols: list[str], rows: list[list], sql: str,
               codes: dict[str, list[str]] | None = None) -> str:
    info = select_columns(sql)
    if len(info) != len(cols):
        info = [(c, "number", False) for c in cols]
    labels = [i[0] for i in info]
    kinds = [i[1] for i in info]
    summable = [i[2] for i in info]
    # A single plain number takes its unit from the question.
    if len(cols) == 1 and kinds[0] == "number":
        kinds[0] = ("pct" if PCT.search(question) else "count" if COUNTISH.search(question)
                    else "money" if MONEY.search(question) else "number")
    if not rows:
        return "No rows match (the result is empty)."
    if len(rows) == 1 and len(cols) == 1:
        return fmt(rows[0][0], kinds[0])

    is_num = [all(isinstance(r[i], (int, float, decimal.Decimal)) or r[i] is None for r in rows)
              for i in range(len(cols))]
    keys = key_columns(sql)
    if len(keys) == len(cols) and any(keys) and not all(keys):
        is_num = [num and not k for num, k in zip(is_num, keys)]  # grouping or text columns name the row
    elif len(rows) > 1 and all(is_num) and len(cols) > 1:
        is_num[0] = False  # a numeric first column (e.g. category code) names the row
    name_idx = [i for i, n in enumerate(is_num) if not n]
    num_idx = [i for i, n in enumerate(is_num) if n]
    nice = question_labels(question, len(num_idx))
    if nice:
        for j, i in enumerate(num_idx):
            labels[i] = nice[j]

    if len(rows) == 1:
        return "\n".join(f"- {labels[i]}: {fmt(rows[0][i], kinds[i])}" for i in range(len(cols)))

    def code(v):
        text = fmt(v, "number")
        meaning = (codes or {}).get(text) or (codes or {}).get(str(v))
        return f"{text} ({meaning[0]})" if meaning else text

    def name(r):
        return " / ".join(code(r[i]) for i in name_idx)

    # Without an ORDER BY the row order is arbitrary: list largest first so numbering is stable.
    if num_idx and not re.search(r"(?i)order\s+by", sql.split(")")[-1] if ")" in sql else sql):
        k0 = num_idx[0]
        rows = sorted(rows, key=lambda r: (r[k0] is None, -(float(r[k0]) if r[k0] is not None else 0),
                                           " / ".join(str(r[i]) for i in name_idx)))

    if not num_idx:  # a list of names only: alphabetical, so the numbering is stable
        rows = sorted(rows, key=name)
    lines = []
    for n, r in enumerate(rows[:MAX_ROWS], 1):
        vals = ", ".join((f"{labels[i]} " if len(num_idx) > 1 else "") + fmt(r[i], kinds[i]) for i in num_idx)
        lines.append(f"{n}. {name(r)}: {vals}" if name_idx and vals else f"{n}. {name(r)}" if name_idx else f"{n}. {vals}")
    if len(rows) > MAX_ROWS:
        lines.append(f"... and {len(rows) - MAX_ROWS} more rows ({len(rows)} in total).")

    if num_idx and name_idx:
        i, k = num_idx[0], kinds[num_idx[0]]
        valued = [r for r in rows if r[i] is not None]
        if len(valued) >= 2 and len({float(r[i]) for r in valued}) > 1:
            hi = max(valued, key=lambda r: float(r[i]))
            lo = min(valued, key=lambda r: float(r[i]))
            what = f" {labels[i]}" if len(num_idx) > 1 else ""
            insight = f"Highest{what}: {name(hi)} ({fmt(hi[i], k)}). Lowest{what}: {name(lo)} ({fmt(lo[i], k)})."
            if summable[i]:
                insight += f" Total: {fmt(sum(float(r[i]) for r in valued), k)}."
            lines.append(insight)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Writing into chat.yaml without touching anything else
# ---------------------------------------------------------------------------

def block(answer: str, indent: str) -> list[str]:
    return [f"{indent}expected_answer: |"] + [f"{indent}  {line}" for line in answer.split("\n")]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    args = ap.parse_args()
    ds = load_dataset(args.dataset)
    path = ds.goldens_dir / "chat.yaml"
    cases = {c.id: c for c in load_cases(ds)}
    ref = Reference(ds)

    answers: dict[tuple[str, int], str] = {}
    for c in cases.values():
        for n, t in enumerate(c.turns):
            if t.expected_sql:
                cols, rows = ref.run(" ".join(t.expected_sql.split()))
                answers[(c.id, n)] = answer_for(t.question, cols, rows, " ".join(t.expected_sql.split()), t.labels)

    lines = path.read_text(encoding="utf-8").split("\n")
    out: list[str] = []
    case_id, turn = None, -1
    i = 0
    while i < len(lines):
        line = lines[i]
        m = re.match(r"^  - id: (\S+)", line)
        if m:
            case_id, turn = m.group(1), -1
        if re.match(r"^      - question:", line):
            turn += 1
        # drop an existing expected_answer block (it is regenerated below)
        if re.match(r"^        expected_answer:", line):
            i += 1
            while i < len(lines) and lines[i].startswith("          "):
                i += 1
            continue
        out.append(line)
        if re.match(r"^        match:", line) and (case_id, turn) in answers:
            out += block(answers[(case_id, turn)], "        ")
        i += 1
    path.write_text("\n".join(out), encoding="utf-8")
    print(f"expected_answer written for {len(answers)} turns in {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
