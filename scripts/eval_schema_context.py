"""Schema-context evaluation: the generator (Haiku). Nothing is saved in the app.

    uv run python scripts/eval_schema_context.py --dataset tgspdcl_meter_readings
    uv run python scripts/eval_schema_context.py --dataset tgspdcl_meter_readings --no-judge
    uv run python scripts/eval_schema_context.py --dataset tgspdcl_meter_readings --step context   # or: columns

Results stay LOCAL ONLY (they contain real column names): results/<dataset>/<run>/.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import bootstrap  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--step", choices=["both", "columns", "context"], default="both")
    ap.add_argument("--no-judge", action="store_true")
    args = ap.parse_args()

    bootstrap.setup()
    from accounts.models import User
    from catalog.models import DataSource, SourceObject

    from evals.config import load_dataset
    from evals.tools_eval import Reference
    from evals.schema_context_eval import (blank_draft, check_code_mappings, check_coverage, check_dates, check_model,
                              check_personal, check_sql_runs, check_validator, column_suggestions, judge_columns,
                              judge_table, run_step3, run_step5)
    from evals.profile import profile_table
    from evals.results import RunWriter

    ds = load_dataset(args.dataset)
    p3 = ds.raw.get("schema_context") or {}
    tables = p3.get("tables") or [ds.source.get("reference_table")]
    personal = p3.get("personal_columns") or []
    model = os.getenv("SCHEMA_CONTEXT_MODEL", "claude-haiku-5-5")
    source = DataSource.objects.get(tenant__slug=ds.tenant_slug, source_id=ds.source_id)
    user = User.objects.get(email=ds.user("full_access").email)
    ref = Reference(ds)
    writer = RunWriter(ds.name, "schema-context")
    table = tables[0]  # the reference CSV holds one table

    print(f"Profiling reference data for '{table}'...")
    profile = profile_table(ref, ds.source.get("reference_table") or table)
    columns = list(profile["columns"])

    runs = []
    if args.step in ("both", "columns"):
        print("Step 1 - column suggestions (Haiku)...")
        runs.append(run_step3(SourceObject.objects.get(source=source, object_name=table), user))
    if args.step in ("both", "context"):
        print("Step 2 - whole draft context (Haiku)...")
        draft = blank_draft(source, tables)
        runs.append(run_step5(source, draft, user))

    judge = None
    if not args.no_judge:
        from evals.judge import get_judge
        judge = get_judge()

    summary = {"steps": {}}
    for run in runs:
        print(f"\n=== {run.step} ({run.duration_ms / 1000:.0f}s) ===")
        if run.error:
            print(f"  ERROR: {run.error}")
            summary["steps"][run.step] = {"error": run.error}
            writer.write_case(run.step, {"run": run})
            continue
        sugg = column_suggestions(run, table)
        results = [check_model(run, model), check_coverage(sugg, columns), check_personal(sugg, personal),
                   check_dates(sugg, profile)]
        if run.step == "schema_context":
            ctx = run.output.get("context") or {}
            results += [check_code_mappings(ctx.get("code_mappings") or {}, profile, ref,
                                            ds.source.get("reference_table") or table),
                        check_sql_runs(run.output, ds, table),
                        check_validator(draft, run.output, writer.dir)]
        usage = run.output.get("usage") or {}
        print(f"  Haiku: {usage.get('input_tokens', 0):,} tokens in / {usage.get('output_tokens', 0):,} out, "
              f"${usage.get('cost_usd', 0):.4f}, sample rows sent: {run.output.get('rows_sent')}")
        for r in results:
            print(f"  {'PASS' if r.passed else 'FAIL'}  {r.name}: {r.detail}")

        col_scores, table_score, judge_cost = {}, None, 0.0
        if judge:
            print("  Judging each column (Opus)...")
            col_metrics = judge_columns(judge, sugg, profile, table)
            for col, m in col_metrics.items():
                col_scores[col] = {"score": m.score, "passed": m.success, "reason": m.reason,
                                   "error": getattr(m, "error", None)}
                judge_cost += m.evaluation_cost or 0.0
            if run.step == "schema_context":
                tm = judge_table(judge, run.output, profile, table)
                table_score = {"score": tm.score, "passed": tm.success, "reason": tm.reason,
                               "error": getattr(tm, "error", None)}
                judge_cost += tm.evaluation_cost or 0.0
            scored = [v for v in col_scores.values() if v["score"] is not None]
            low = sorted(((c, v) for c, v in col_scores.items() if v["score"] is not None and not v["passed"]),
                         key=lambda x: x[1]["score"])
            avg = sum(v["score"] for v in scored) / len(scored) if scored else 0
            print(f"  Column descriptions: {sum(v['passed'] for v in scored)}/{len(scored)} pass, average {avg:.2f}")
            for c, v in low:
                s = sugg.get(c, {})
                print(f"    {v['score']:.2f}  {c} (role {s.get('role')}, {s.get('sensitivity')}): {(v['reason'] or '')[:220]}")
            if table_score:
                print(f"  Table context: {table_score['score']:.2f} -> {'PASS' if table_score['passed'] else 'FAIL'}")
                print(f"    {(table_score['reason'] or '')[:500]}")
            print(f"  Judge cost: ${judge_cost:.2f}")

        writer.write_case(run.step, {"run": run, "checks": results, "column_scores": col_scores,
                                     "table_score": table_score, "suggestions": sugg})
        summary["steps"][run.step] = {
            "checks": {r.name: {"passed": r.passed, "detail": r.detail} for r in results},
            "columns_passed": sum(1 for v in col_scores.values() if v.get("passed")),
            "columns_judged": len(col_scores), "table_score": table_score, "judge_cost_usd": judge_cost,
            "haiku_cost_usd": usage.get("cost_usd"),
        }

    writer.write_case("profile", profile)
    writer.write_summary(summary)
    print(f"\nFull results (local only): {writer.dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
