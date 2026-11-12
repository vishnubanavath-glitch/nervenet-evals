"""Check the golden files of a dataset. Nothing is sent to the app or to any AI; Postgres is not needed.

    uv run python scripts/check_goldens.py --dataset tgspdcl_meter_readings            # full check + summary
    uv run python scripts/check_goldens.py --dataset tgspdcl_meter_readings --show     # also print every question
    uv run python scripts/check_goldens.py --dataset tgspdcl_meter_readings --show --only M003,G020
    uv run python scripts/check_goldens.py --dataset tgspdcl_meter_readings --show --level hard
    uv run python scripts/check_goldens.py --dataset tgspdcl_meter_readings --file tools

Checks
  chat.yaml   valid YAML, unique ids, every turn has a question, match is rows/contains/none,
              data turns have expected_sql, behaviour turns have expected_behavior,
              every expected_sql runs on the reference CSV and returns rows,
              every expected_answer matches what the CSV gives today (else: refresh it).
  tools.yaml  valid YAML, unique ids, allowed expect values and their extra fields,
              every {c:column} exists in the CSV, every answer-key SQL runs and returns rows.
Exit code 0 = all good, 1 = problems found.
"""
from __future__ import annotations

import argparse
import csv
import importlib.util
import re
import sys
import time
from collections import Counter, OrderedDict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import yaml  # noqa: E402

from evals.config import load_dataset  # noqa: E402
from evals.tools_eval import Reference  # noqa: E402

# Reuse the exact generator that writes expected_answer, to detect stale answers.
_spec = importlib.util.spec_from_file_location("wea", ROOT / "scripts" / "write_expected_answers.py")
wea = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(wea)

TOOLS_EXPECT = {"match_reference", "rejected", "masked", "masked_headers", "row_limit", "capped",
                "kpi_matches", "not_listed"}
LEVEL_ORDER = ["easy", "medium", "hard", "hardest", "god", "feature", "conversation", "robustness"]


class Report:
    def __init__(self):
        self.problems: list[str] = []
        self.warnings: list[str] = []

    def bad(self, msg: str):
        self.problems.append(msg)

    def warn(self, msg: str):
        self.warnings.append(msg)


def one_line(sql: str) -> str:
    return " ".join(sql.split())


def real_sql(text: str) -> str:
    """tools.yaml placeholders -> real names (no app catalog needed)."""
    text = re.sub(r"\{c:([^}]+)\}", lambda m: f'"{m.group(1)}"', text)
    return re.sub(r"\{t:([^}]+)\}", lambda m: m.group(1), text)


# ---------------------------------------------------------------------------
# chat.yaml
# ---------------------------------------------------------------------------

def check_chat(ds, ref: Reference, rep: Report, args) -> dict:
    path = ds.goldens_dir / "chat.yaml"
    if not path.exists():
        rep.bad("chat.yaml not found")
        return {}
    try:
        cases = yaml.safe_load(path.read_text(encoding="utf-8")).get("cases") or []
    except yaml.YAMLError as exc:
        rep.bad(f"chat.yaml is not valid YAML: {exc}")
        return {}

    ids = [c.get("id") for c in cases]
    for cid, n in Counter(ids).items():
        if n > 1:
            rep.bad(f"chat {cid}: id used {n} times")

    stats = {"cases": len(cases), "turns": 0, "sql_ok": 0, "answers_ok": 0, "by_level": Counter()}
    wanted = {x.strip() for x in args.only.split(",")} if args.only else None
    for c in cases:
        cid = c.get("id") or "?"
        level = c.get("level", "-")
        stats["by_level"][level] += 1
        turns = c.get("turns") or []
        if not turns:
            rep.bad(f"chat {cid}: no turns")
            continue
        labels = {str(k): [str(x) for x in v] for k, v in (c.get("labels") or {}).items()}
        show = args.show and (not wanted or cid in wanted) and (not args.level or level == args.level)
        if show:
            print(f"\n{'=' * 100}\n{cid}  [{level}]  tags: {', '.join(c.get('tags') or [])}")
            if c.get("notes"):
                print(f"note: {' '.join(str(c['notes']).split())}")
        for n, t in enumerate(turns, 1):
            stats["turns"] += 1
            where = f"chat {cid}" + (f" turn {n}" if len(turns) > 1 else "")
            q = t.get("question")
            if not q:
                rep.bad(f"{where}: missing question")
            match = t.get("match", "rows" if t.get("expected_sql") else "none")
            if match not in ("rows", "contains", "none"):
                rep.bad(f"{where}: match must be rows, contains or none (is {match!r})")
            if match != "none" and not t.get("expected_sql"):
                rep.bad(f"{where}: data turn without expected_sql")
            if not t.get("expected_sql") and not t.get("expected_behavior"):
                rep.bad(f"{where}: needs expected_sql or expected_behavior")
            if show:
                print(f"\n  Q{n if len(turns) > 1 else ''}: {' '.join(str(q).split())}")
                if t.get("expect"):
                    print(f"  also expected: {t['expect']}")
                if t.get("expected_behavior"):
                    print(f"  behaviour: {' '.join(t['expected_behavior'].split())}")
            sql = t.get("expected_sql")
            if not sql:
                continue
            try:
                cols, rows = ref.run(one_line(sql))
            except Exception as exc:
                rep.bad(f"{where}: expected_sql fails on the CSV: {str(exc).splitlines()[0][:160]}")
                continue
            stats["sql_ok"] += 1
            if not rows:
                rep.warn(f"{where}: expected_sql returns no rows")
            fresh = wea.answer_for(q, cols, rows, one_line(sql), labels)
            stored = (t.get("expected_answer") or "").strip()
            if not stored:
                rep.bad(f"{where}: no expected_answer (run scripts/write_expected_answers.py)")
            elif stored != fresh.strip():
                rep.bad(f"{where}: expected_answer is out of date with the CSV (run scripts/write_expected_answers.py)")
            else:
                stats["answers_ok"] += 1
            if show:
                print(f"  match: {match}")
                print("  expected answer:")
                for line in fresh.strip().splitlines():
                    print(f"    {line}")
    return stats


# ---------------------------------------------------------------------------
# tools.yaml
# ---------------------------------------------------------------------------

def check_tools(ds, ref: Reference, rep: Report, args, csv_columns: set[str]) -> dict:
    path = ds.goldens_dir / "tools.yaml"
    if not path.exists():
        rep.bad("tools.yaml not found")
        return {}
    text = path.read_text(encoding="utf-8")
    try:
        cases = yaml.safe_load(text).get("cases") or []
    except yaml.YAMLError as exc:
        rep.bad(f"tools.yaml is not valid YAML: {exc}")
        return {}
    for cid, n in Counter(c.get("id") for c in cases).items():
        if n > 1:
            rep.bad(f"tools {cid}: id used {n} times")

    stats = {"cases": len(cases), "sql_ok": 0, "by_area": Counter()}
    wanted = {x.strip() for x in args.only.split(",")} if args.only else None
    for c in cases:
        cid = c.get("id") or "?"
        stats["by_area"][c.get("area", "-")] += 1
        where = f"tools {cid}"
        exp_ = c.get("expect")
        if exp_ not in TOOLS_EXPECT:
            rep.bad(f"{where}: expect must be one of {sorted(TOOLS_EXPECT)} (is {exp_!r})")
        if not any(k in c for k in ("sql", "raw_sql", "raw_sql_template", "tool")):
            rep.bad(f"{where}: needs sql, raw_sql, raw_sql_template or tool")
        if exp_ == "row_limit" and "rows" not in c:
            rep.bad(f"{where}: row_limit needs 'rows'")
        if exp_ == "capped" and "limit" not in c:
            rep.bad(f"{where}: capped needs 'limit'")
        if exp_ == "kpi_matches" and "kpi_column" not in c:
            rep.bad(f"{where}: kpi_matches needs 'kpi_column'")
        if exp_ == "not_listed" and "other_source" not in c:
            rep.bad(f"{where}: not_listed needs 'other_source'")
        for field in ("sql", "raw_sql_template"):
            for col in re.findall(r"\{c:([^}]+)\}", str(c.get(field) or "")):
                if col not in csv_columns:
                    rep.bad(f"{where}: unknown column {{c:{col}}}")
        show = args.show and args.file in ("both", "tools") and (not wanted or cid in wanted)
        if show:
            print(f"\n{'-' * 100}\n{cid}  [{c.get('area')}]  {c.get('title')}  -> expect {exp_}")
            if c.get("notes"):
                print(f"  note: {c['notes']}")
            body = c.get("sql") or c.get("raw_sql") or c.get("raw_sql_template") or f"{c.get('tool')} {c.get('args')}"
            print(f"  sends: {one_line(str(body))}")
        if exp_ in ("match_reference", "kpi_matches") and c.get("sql"):
            try:
                cols, rows = ref.run(one_line(real_sql(c["sql"])))
                stats["sql_ok"] += 1
                if not rows:
                    rep.warn(f"{where}: answer-key SQL returns no rows")
                if exp_ == "kpi_matches" and c.get("kpi_column") not in cols:
                    rep.bad(f"{where}: kpi_column {c.get('kpi_column')!r} is not a result column {cols}")
                if show:
                    print(f"  expected rows: {rows[:8]}{' ...' if len(rows) > 8 else ''}")
            except Exception as exc:
                rep.bad(f"{where}: answer-key SQL fails on the CSV: {str(exc).splitlines()[0][:160]}")
    return stats


# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--file", choices=["both", "chat", "tools"], default="both")
    ap.add_argument("--show", action="store_true", help="print each question with its expected answer")
    ap.add_argument("--only", help="comma-separated ids to show")
    ap.add_argument("--level", help="show only this chat level (easy, medium, hard, hardest, god, ...)")
    args = ap.parse_args()

    ds = load_dataset(args.dataset)
    started = time.time()
    rep = Report()
    print(f"Checking golden files of '{ds.name}' against {ds.reference_file}")
    with open(ds.reference_file, encoding="utf-8", newline="") as fh:
        csv_columns = set(next(csv.reader(fh)))
    ref = Reference(ds)

    chat = check_chat(ds, ref, rep, args) if args.file in ("both", "chat") else {}
    tools = check_tools(ds, ref, rep, args, csv_columns) if args.file in ("both", "tools") else {}

    print("\n" + "=" * 100 + "\nSUMMARY")
    if chat:
        print(f"chat.yaml : {chat['cases']} questions, {chat['turns']} turns")
        levels = OrderedDict((k, chat["by_level"][k]) for k in LEVEL_ORDER if chat["by_level"].get(k))
        print("            " + " · ".join(f"{k} {v}" for k, v in levels.items()))
        print(f"            answer-key SQL ran: {chat['sql_ok']} · expected answers up to date: {chat['answers_ok']}")
    if tools:
        print(f"tools.yaml: {tools['cases']} tests")
        print("            " + " · ".join(f"{k} {v}" for k, v in tools["by_area"].items()))
        print(f"            answer-key SQL ran: {tools['sql_ok']}")
    print(f"time: {time.time() - started:.0f}s")

    if rep.warnings:
        print(f"\nWarnings ({len(rep.warnings)}):")
        for w in rep.warnings:
            print(f"  ! {w}")
    if rep.problems:
        print(f"\nPROBLEMS ({len(rep.problems)}):")
        for p in rep.problems:
            print(f"  x {p}")
        return 1
    print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
