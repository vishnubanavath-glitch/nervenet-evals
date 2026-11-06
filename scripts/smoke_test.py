"""Smoke test: one question through the real chat, end to end.

    uv run python scripts/smoke_test.py --dataset tgspdcl_meter_readings [--user full_access]

Proves the pipeline: chat (Sonnet) -> MCP tools -> trace -> code checks ->
Opus judge -> local result file -> (optional) masked upload to Confident AI.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import bootstrap  # noqa: E402

QUESTION = "How many meter reading records are there in total in the meter readings data?"


def expected_row_count(ds) -> int | None:
    if not ds.reference_file:
        return None
    import duckdb

    path = ds.reference_file.replace("'", "''")
    return duckdb.connect().execute(f"SELECT count(*) FROM read_csv('{path}', all_varchar = true)").fetchone()[0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--user", default="full_access")
    ap.add_argument("--question", default=QUESTION)
    args = ap.parse_args()

    bootstrap.setup()
    from deepeval import evaluate
    from deepeval.evaluate.configs import AsyncConfig, DisplayConfig
    from deepeval.metrics import AnswerRelevancyMetric
    from deepeval.test_case import LLMTestCase, ToolCall

    from evals import checks
    from evals.chat_runner import run_conversation
    from evals.config import load_dataset
    from evals.judge import get_judge
    from evals.results import RunWriter, answer_for_judge, masked_tool_calls, usage_totals

    ds = load_dataset(args.dataset)
    chat_model = os.getenv("ANTHROPIC_MODEL_NAME", "claude-sonnet-5-5")

    print(f"Q: {args.question}\n   (dataset {ds.name}, user {args.user})")
    conv = run_conversation(ds, args.user, [args.question], title="smoke test")
    turn = conv.turns[0]
    trace = turn.trace

    # --- code checks -------------------------------------------------------------
    results = [
        checks.check_models(trace, chat_model),
        checks.check_no_real_names_to_model(trace, ds),
        checks.check_no_sensitive_values_to_model(trace, ds),
        checks.check_discover_before_query(trace),
    ]
    expected = expected_row_count(ds)
    if expected is not None:
        shown = [row for t in turn.ui_tables for row in (t.get("rows") or [])]
        values = [v for row in shown for v in (row.values() if isinstance(row, dict) else row)]
        hit = any(str(v).replace(",", "") == str(expected) for v in values)
        results.append(checks.CheckResult("rows_match_data", hit,
                                          f"expected {expected:,}; {'found' if hit else 'not found'} in result rows"))

    # --- judge (masked test case: only what the model saw) --------------------------
    masked_tools = masked_tool_calls(trace)
    usage = usage_totals(trace)
    test_case = LLMTestCase(
        name="smoke-test",
        input=args.question,
        actual_output=answer_for_judge(turn.answer) or "(no answer)",
        tools_called=[ToolCall(name=t["name"], input_parameters=t["arguments"], output=t["result_seen_by_model"])
                      for t in masked_tools],
        additional_metadata={
            "dataset": ds.name, "phase": "smoke-test", "user": args.user,
            "models_used": trace["models_used"], "duration_ms": turn.duration_ms, **usage,
            "checks": {r.name: {"passed": r.passed, "detail": r.detail} for r in results},
        },
    )
    metric = AnswerRelevancyMetric(model=get_judge(), threshold=0.5)
    evaluation = evaluate(
        [test_case], [metric],
        async_config=AsyncConfig(run_async=False),
        display_config=DisplayConfig(print_results=False, show_indicator=False),
    )
    m = evaluation.test_results[0].metrics_data[0]

    # --- local record (full) ------------------------------------------------------------
    writer = RunWriter(ds.name, "smoke-test")
    path = writer.write_case("smoke", {
        "question": args.question, "conversation": conv,
        "checks": results, "judge": {"metric": m.name, "score": m.score, "reason": m.reason,
                                      "cost_usd": m.evaluation_cost, "judge_model": m.evaluation_model},
    })
    writer.write_summary({"cases": 1, "checks_passed": all(r.passed for r in results), "judge_score": m.score})

    # --- terminal ----------------------------------------------------------------------
    print(f"\nModel(s) used : {', '.join(trace['models_used']) or 'none'}")
    print(f"Tools         : {' -> '.join(t['name'] for t in masked_tools) or 'none'}")
    for sql in checks.model_written_sql(trace):
        print(f"SQL (masked)  : {sql}")
    print(f"Answer        : {(turn.answer or '').strip()[:400]}")
    if turn.error:
        print(f"ERROR         : {turn.error.get('error')}")
    for w in turn.warnings:
        print(f"Warning       : {w}")
    print("\nChecks")
    for r in results:
        print(f"  {'PASS' if r.passed else 'FAIL'}  {r.name}: {r.detail}")
    print(f"\nJudge ({m.evaluation_model}) {m.name}: {m.score} -> {'PASS' if m.success else 'FAIL'}")
    print(f"  reason: {m.reason}")
    print(f"\nLLM calls: {usage['llm_calls']} · tokens in {usage['total_input_tokens']:,}"
          f" ({usage['cache_read_input_tokens']:,} from cache) / out {usage['output_tokens']:,}"
          f" · judge cost ${m.evaluation_cost or 0:.4f} · {turn.duration_ms / 1000:.1f}s")
    print(f"Full trace: {path}")
    print("Dashboard : " + ("uploaded to Confident AI (masked)" if os.getenv("CONFIDENT_API_KEY") else "not configured"))
    ok = all(r.passed for r in results) and m.success and not turn.error
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
