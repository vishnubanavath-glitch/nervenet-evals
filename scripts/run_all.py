"""Run the whole evaluation with one command: setup check, then the chosen evaluations, then one summary.

    uv run python scripts/run_all.py --dataset tgspdcl_meter_readings                    # everything
    uv run python scripts/run_all.py --dataset tgspdcl_meter_readings --only tools,chat  # some of them
    uv run python scripts/run_all.py --dataset tgspdcl_meter_readings --no-judge         # skip Opus (cheap)

Evaluations: tools = MCP tools (free), chat = chat responses (~$7 with judge),
schema-context = schema-context generator (~$1.50).
Each evaluation still writes its own results folder; this prints one combined summary at the end.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

EVALS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(EVALS_DIR))

EVALS = {
    "tools": ("Tools (MCP, no AI)", "eval_tools.py", "tools"),
    "chat": ("Chat responses (Sonnet)", "eval_chat.py", "chat"),
    "schema-context": ("Schema-context generator (Haiku)", "eval_schema_context.py", "schema-context"),
}


def run(script: str, args: list[str]) -> tuple[int, float]:
    started = time.time()
    code = subprocess.call([sys.executable, str(EVALS_DIR / "scripts" / script), *args], cwd=EVALS_DIR)
    return code, time.time() - started


def latest_summary(dataset: str, suffix: str, after: float) -> tuple[Path | None, dict]:
    base = EVALS_DIR / "results" / dataset
    runs = [p for p in base.glob(f"*_{suffix}") if p.stat().st_mtime >= after - 5]
    if not runs:
        return None, {}
    run_dir = sorted(runs)[-1]
    s = run_dir / "summary.json"
    return run_dir, (json.loads(s.read_text(encoding="utf-8")) if s.exists() else {})


def describe(kind: str, summary: dict) -> list[str]:
    if not summary:
        return ["no summary written (see the output above)"]
    if kind == "tools":
        c = summary.get("counts", {})
        return [f"{c.get('PASS', 0)} passed, {c.get('FAIL', 0)} failed, {c.get('ERROR', 0)} errors, "
                f"{c.get('SKIP', 0)} skipped", f"failed: {', '.join(summary.get('failed', [])) or 'none'}"]
    if kind == "chat":
        lines = [f"data correct {summary.get('data_pass')}/{summary.get('data_total')} turns; "
                 f"all code checks {summary.get('all_checks_pass')}/{summary.get('turns')}; "
                 f"governance clean {summary.get('governance_pass')}/{summary.get('turns')}"]
        scores = summary.get("judge_scores") or {}
        for metric in ("Correctness [GEval]", "Answer Relevancy", "Faithfulness", "Knowledge Retention",
                       "Conversation Completeness"):
            vals = [v[metric] for v in scores.values() if metric in v and v[metric].get("score") is not None]
            if vals:
                avg = sum(v["score"] for v in vals) / len(vals)
                lines.append(f"{metric}: average {avg:.2f}, {sum(1 for v in vals if v.get('passed'))}/{len(vals)} pass")
        lines.append(f"cost: chat ${summary.get('chat_cost_usd', 0):.2f} + judge ${summary.get('judge_cost_usd', 0):.2f}")
        return lines
    if kind == "schema-context":
        lines = []
        for step, s in (summary.get("steps") or {}).items():
            if s.get("error"):
                lines.append(f"{step}: ERROR {s['error'][:120]}")
                continue
            failed = [k for k, v in (s.get("checks") or {}).items() if not v.get("passed")]
            line = f"{step}: checks {'all pass' if not failed else 'FAILED ' + ', '.join(failed)}"
            if s.get("columns_judged"):
                line += f"; columns {s['columns_passed']}/{s['columns_judged']} pass"
            if s.get("table_score"):
                line += f"; table {s['table_score'].get('score')}"
            lines.append(line)
        return lines or ["no steps recorded"]
    return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True)
    ap.add_argument("--only", default="tools,chat,schema-context", help="comma-separated: tools,chat,schema-context")
    ap.add_argument("--no-judge", action="store_true", help="skip the Opus judge (chat and schema-context)")
    ap.add_argument("--skip-setup-check", action="store_true")
    args = ap.parse_args()

    chosen = [p.strip() for p in args.only.split(",") if p.strip()]
    unknown = [p for p in chosen if p not in EVALS]
    if unknown:
        raise SystemExit(f"Unknown evaluation(s) {unknown}; use: {', '.join(EVALS)}")
    common = ["--dataset", args.dataset]
    started_all = time.time()

    if not args.skip_setup_check:
        print("=" * 80 + "\nSetup check\n" + "=" * 80)
        code, _ = run("check_setup.py", common)
        if code != 0:
            print("\nSetup check failed: fix the FAIL lines above, then run again. Nothing was run.")
            return 1

    report = []
    for p in chosen:
        name, script, suffix = EVALS[p]
        print("\n" + "=" * 80 + f"\n{name}\n" + "=" * 80)
        extra = ["--no-judge"] if args.no_judge and p in ("chat", "schema-context") else []
        t0 = time.time()
        code, secs = run(script, common + extra)
        run_dir, summary = latest_summary(args.dataset, suffix, t0)
        report.append((p, name, code, secs, run_dir, summary))

    print("\n" + "=" * 80 + f"\nCOMBINED SUMMARY - dataset {args.dataset}\n" + "=" * 80)
    for p, name, code, secs, run_dir, summary in report:
        print(f"\n{name}  ({secs / 60:.1f} min{', finished with failures' if code else ''})")
        for line in describe(p, summary):
            print(f"  - {line}")
        if run_dir:
            print(f"  results: {run_dir}")
    print(f"\nTotal time: {(time.time() - started_all) / 60:.1f} min")
    return 0 if all(code == 0 for _, _, code, *_ in report) else 1


if __name__ == "__main__":
    sys.exit(main())
