"""Chat evaluation: golden questions through the real in-app chat (Sonnet), graded by code + Opus.

    uv run python scripts/eval_chat.py --dataset tgspdcl_meter_readings --preview     # questions + expected answers
    uv run python scripts/eval_chat.py --dataset tgspdcl_meter_readings               # chat + judge
    uv run python scripts/eval_chat.py --dataset tgspdcl_meter_readings --only C01,M01
    uv run python scripts/eval_chat.py --dataset tgspdcl_meter_readings --no-judge    # chat + code checks only
    uv run python scripts/eval_chat.py --dataset tgspdcl_meter_readings --judge-only latest   # (re)judge a saved run

Each case is saved as soon as it has run, so a failed judge step never loses the chat results.
Goldens: datasets/<dataset>/goldens/chat.yaml. Results: results/<dataset>/<run>/.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import bootstrap  # noqa: E402


def short(v, n=150) -> str:
    s = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[: n - 3] + "..."


def fmt_rows(rows, limit=12) -> str:
    def cell(v):
        if isinstance(v, float):
            return f"{v:,.2f}"
        if isinstance(v, int):
            return f"{v:,}"
        return "(empty)" if v is None else str(v)
    lines = ["; ".join(cell(v) for v in r) for r in rows[:limit]]
    more = f"<br>... and {len(rows) - limit} more" if len(rows) > limit else ""
    return "<br>".join(lines) + more


def write_review(cases, path: Path) -> None:
    out = ["# Chat golden questions: review", "",
           "For each question, check: **would a real user ask this?**, **is the expected answer right?**, "
           "and **is the note's meaning correct?** Mark each ✅ keep / ✏️ change / ❌ remove, and add any questions you want.",
           "", "Expected answers are calculated from the CSV. *rows* = the table must have exactly these rows; "
           "*contains* = these rows must appear (more allowed).", "",
           "| # | Question | Expected answer | Note to confirm | Your verdict |", "|---|---|---|---|---|"]
    for c in cases:
        for i, t in enumerate(c.turns, 1):
            cid = c.id + (f" turn {i}" if c.multi_turn else "")
            exp = (f"*{t.match}*<br>{fmt_rows(t.expected['rows'])}" if t.expected
                   else "*behaviour:* " + " ".join((t.expected_behavior or "").split()))
            note = c.notes if i == 1 else ""
            out.append(f"| {cid} | {t.question.replace('|', '/')} | {exp.replace('|', '/')} | {note.replace('|', '/')} | |")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def preview(cases) -> None:
    for c in cases:
        print(f"\n{c.id}  [{c.kind}]" + (f"   note: {c.notes}" if c.notes else ""))
        for i, t in enumerate(c.turns, 1):
            print((f"  turn {i}: " if c.multi_turn else "  Q: ") + t.question)
            if t.expected:
                print(f"     expected ({t.match}): {short(t.expected['rows'])}")
            elif t.expected_behavior:
                print(f"     expected behaviour: {short(' '.join(t.expected_behavior.split()), 220)}")


# ---------------------------------------------------------------------------
# Chat stage
# ---------------------------------------------------------------------------

def run_chat(ds, cases, writer) -> None:
    from evals import checks
    from evals.chat_runner import run_conversation
    from evals.chat_eval import chat_cost_usd, check_data, check_tools_ok, expected_text, tool_context
    from evals.results import answer_for_judge, masked_tool_calls, usage_totals

    chat_model = os.getenv("ANTHROPIC_MODEL_NAME", "claude-sonnet-5-5")
    for case in cases:
        print(f"\n{case.id} [{case.kind}] {case.turns[0].question}")
        try:
            conv = run_conversation(ds, case.user, [t.question for t in case.turns], title=case.id, between_turns=True)
        except Exception as exc:
            print(f"   ERROR running chat: {exc}")
            writer.write_case(case.id, {"id": case.id, "kind": case.kind, "error": str(exc)})
            continue
        rec: dict[str, Any] = {"id": case.id, "kind": case.kind, "notes": case.notes,
                               "session_id": conv.session_id, "turns": []}
        discovered_earlier = False
        for n, (gt, turn) in enumerate(zip(case.turns, conv.turns), 1):
            trace = turn.trace
            order = checks.check_discover_before_query(trace)
            if not order.passed and discovered_earlier:
                # A follow-up turn may reuse what an earlier turn of the same chat already explored.
                order = checks.CheckResult(order.name, True, order.detail + " (explored in an earlier turn)")
            discovered_earlier = discovered_earlier or any(tc["name"] == "discover" for tc in trace.get("tool_calls", []))
            results = [check_data(gt, turn.ui_tables, turn.answer), checks.check_models(trace, chat_model),
                       check_tools_ok(trace, turn), order]
            governance = [checks.check_no_real_names_to_model(trace, ds),
                          checks.check_no_sensitive_values_to_model(trace, ds)]
            usage, cost = usage_totals(trace), chat_cost_usd(trace)
            answer, tools = answer_for_judge(turn.answer), masked_tool_calls(trace)
            label = f"turn {n}: " if case.multi_turn else ""
            print(f"   {label}" + "  ".join(f"{r.name}={'PASS' if r.passed else 'FAIL'}" for r in results)
                  + f"  | {usage['llm_calls']} calls, ${cost:.3f}, {turn.duration_ms / 1000:.0f}s")
            for r in results:
                if not r.passed or "recovered" in r.detail:
                    print(f"      - {r.name}: {r.detail}")
            rec["turns"].append({
                "name": case.id + (f"-t{n}" if case.multi_turn else ""),
                "question": gt.question, "match": gt.match, "expected": gt.expected,
                "expected_behavior": gt.expected_behavior, "answer_raw": turn.answer, "answer": answer,
                "checks": results, "governance": governance, "usage": usage, "chat_cost_usd": cost,
                "duration_ms": turn.duration_ms, "sql": checks.model_written_sql(trace),
                # Everything the judge needs, so judging can be re-run without re-asking the chat.
                "judge_input": {"input": gt.question, "actual_output": answer or "(no answer)",
                                "expected_output": expected_text(gt, case.notes),
                                "retrieval_context": tool_context(trace),
                                "tools": [{"name": t["name"], "arguments": t["arguments"]} for t in tools]},
                "turn_detail": turn,
            })
        writer.write_case(case.id, rec)


# ---------------------------------------------------------------------------
# Judge stage (works on saved records)
# ---------------------------------------------------------------------------

def load_records(run_dir: Path) -> dict[str, dict[str, Any]]:
    recs = {}
    for p in sorted(run_dir.glob("*.json")):
        if p.name == "summary.json":
            continue
        recs[p.stem] = json.loads(p.read_text(encoding="utf-8"))
    return recs


def run_judge(ds, records: dict[str, dict[str, Any]]) -> tuple[dict[str, dict[str, Any]], float]:
    from deepeval import evaluate
    from deepeval.evaluate.configs import AsyncConfig, DisplayConfig, ErrorConfig
    from deepeval.test_case import ConversationalTestCase, LLMTestCase, ToolCall, Turn

    from evals.judge import get_judge
    from evals.chat_eval import conversation_metrics, single_turn_metrics

    judge = get_judge()
    singles, convs = [], []
    for cid, rec in records.items():
        if rec.get("error"):
            continue
        conv_turns = []
        for t in rec["turns"]:
            ji = t["judge_input"]
            checks_meta = {c["name"]: c["passed"] for c in t["checks"] + t["governance"]}
            singles.append(LLMTestCase(
                name=t["name"], input=ji["input"], actual_output=ji["actual_output"],
                expected_output=ji["expected_output"], retrieval_context=ji["retrieval_context"],
                tools_called=[ToolCall(name=x["name"], input_parameters=x["arguments"]) for x in ji["tools"]],
                additional_metadata={"dataset": ds.name, "case": cid, "kind": rec["kind"], "checks": checks_meta,
                                     "chat_cost_usd": round(t["chat_cost_usd"], 4), **t["usage"]}))
            conv_turns += [Turn(role="user", content=ji["input"]), Turn(role="assistant", content=ji["actual_output"])]
        if rec["kind"] == "multi-turn":
            convs.append(ConversationalTestCase(name=cid, turns=conv_turns,
                                                additional_metadata={"dataset": ds.name, "case": cid}))

    scores: dict[str, dict[str, Any]] = {}
    cost = 0.0
    display = DisplayConfig(print_results=False, show_indicator=False)
    for tcs, metrics in [(singles, single_turn_metrics(judge)), (convs, conversation_metrics(judge))]:
        if not tcs:
            continue
        ev = evaluate(tcs, metrics, async_config=AsyncConfig(run_async=True, max_concurrent=3),
                      display_config=display, error_config=ErrorConfig(ignore_errors=True))
        for tr in ev.test_results:
            for m in tr.metrics_data or []:
                scores.setdefault(tr.name, {})[m.name] = {"score": m.score, "passed": m.success,
                                                          "reason": m.reason, "error": m.error}
                cost += m.evaluation_cost or 0.0
    return scores, cost


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report(records, scores, judge_cost, writer_dir: Path) -> dict[str, Any]:
    def sc(js, metric):
        v = js.get(metric)
        return "-" if not v else ("ERR" if v.get("error") or v.get("score") is None else f"{v['score']:.2f}")

    totals = {"cases": len(records), "turns": 0, "data_pass": 0, "data_total": 0, "all_checks_pass": 0,
              "governance_pass": 0, "chat_cost_usd": 0.0, "judge_cost_usd": judge_cost}
    print("\n" + "=" * 100)
    print(f"{'case':<8}{'data':<7}{'checks':<8}{'Correct':<9}{'Relevant':<10}{'Faithful':<10}{'Memory':<8}{'Complete':<9}")
    for cid, rec in records.items():
        if rec.get("error"):
            print(f"{cid:<8}ERROR  {rec['error'][:80]}")
            continue
        conv = scores.get(cid, {}) if rec["kind"] == "multi-turn" else {}
        rec["conversation_judge"] = conv or None
        for i, t in enumerate(rec["turns"], 1):
            js = scores.get(t["name"], {})
            t["judge"] = js
            data_ok = t["checks"][0]["passed"] if isinstance(t["checks"][0], dict) else t["checks"][0].passed
            all_ok = all((c["passed"] if isinstance(c, dict) else c.passed) for c in t["checks"])
            gov_ok = all((g["passed"] if isinstance(g, dict) else g.passed) for g in t["governance"])
            totals["turns"] += 1
            totals["chat_cost_usd"] += t["chat_cost_usd"]
            if t["match"] != "none":
                totals["data_total"] += 1
                totals["data_pass"] += int(data_ok)
            totals["all_checks_pass"] += int(all_ok)
            totals["governance_pass"] += int(gov_ok)
            last = i == len(rec["turns"])
            mem = sc(conv, "Knowledge Retention") if conv and last else ""
            comp = sc(conv, "Conversation Completeness") if conv and last else ""
            print(f"{t['name']:<8}{('PASS' if data_ok else 'FAIL') if t['match'] != 'none' else '-':<7}"
                  f"{'PASS' if all_ok else 'FAIL':<8}{sc(js, 'Correctness [GEval]'):<9}{sc(js, 'Answer Relevancy'):<10}"
                  f"{sc(js, 'Faithfulness'):<10}{mem:<8}{comp:<9}")
        (writer_dir / f"{cid}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    def avg(metric):
        vals = [v[metric]["score"] for v in scores.values() if metric in v and v[metric].get("score") is not None]
        return (sum(vals) / len(vals), sum(1 for v in scores.values() if metric in v and v[metric].get("passed")), len(vals)) if vals else None

    print("=" * 100)
    print(f"Data correct     : {totals['data_pass']}/{totals['data_total']} turns")
    print(f"All code checks  : {totals['all_checks_pass']}/{totals['turns']} turns")
    print(f"Governance (names/sensitive to model): {totals['governance_pass']}/{totals['turns']} turns clean")
    for metric in ("Correctness [GEval]", "Answer Relevancy", "Faithfulness", "Knowledge Retention", "Conversation Completeness"):
        a = avg(metric)
        if a:
            print(f"{metric:<26}: average {a[0]:.2f}, {a[1]}/{a[2]} pass")
    print(f"Cost             : chat ${totals['chat_cost_usd']:.2f} + judge ${judge_cost:.2f}")
    print(f"Full results     : {writer_dir}")
    print("Dashboard        : " + ("uploaded to Confident AI (masked)" if os.getenv("CONFIDENT_API_KEY") else "not configured"))
    totals["judge_scores"] = scores
    return totals


def write_remarks_md(records: dict[str, dict[str, Any]], run_dir: Path) -> Path:
    """One readable file with every answer's scores and remarks."""
    out = [f"# Chat evaluation remarks: {run_dir.name}", ""]
    for rec in records.values():
        for t in rec.get("turns", []):
            js = t.get("judge") or {}
            scores = ", ".join(f"{k.replace(' [GEval]', '')} {v['score']:.2f}" for k, v in js.items()
                               if v.get("score") is not None)
            data = next((c for c in t.get("checks", []) if c["name"] == "data_correct"), None)
            out += [f"## {t['name']}: {t['question']}", ""]
            if data and t.get("match") != "none":
                out.append(f"**Data:** {'PASS' if data['passed'] else 'FAIL'} ({data.get('detail', '')})  ")
            if scores:
                out.append(f"**Scores:** {scores}  ")
            out.append("")
            r = t.get("remarks") or {}
            if r.get("error"):
                out += [f"_Remarks failed: {r['error']}_", ""]
                continue
            for title, key in (("What's good", "whats_good"), ("What's bad", "whats_bad"),
                               ("What can be improved", "improvements")):
                items = r.get(key) or []
                out.append(f"**{title}:**" + (" none" if not items else ""))
                out += [f"- {x}" for x in items]
                out.append("")
    path = run_dir / "remarks.md"
    path.write_text("\n".join(out), encoding="utf-8")
    return path


def finish_remarks(ds, records, scores, run_dir: Path, report_after: bool = True) -> float:
    from evals.judge import get_judge
    from evals.remarks import add_remarks, print_remarks

    print("\nWriting remarks (Opus): what's good, what's bad, what can be improved...")
    for rec in records.values():
        for t in rec.get("turns", []):
            t["judge"] = scores.get(t["name"], t.get("judge") or {})
    cost = add_remarks(get_judge(), records, scores)
    for cid, rec in records.items():
        (run_dir / f"{cid}.json").write_text(json.dumps(rec, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    print_remarks(records)
    path = write_remarks_md(records, run_dir)
    print(f"\nAll remarks in one file: {path}")
    return cost


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--preview", action="store_true")
    ap.add_argument("--only")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--judge-only", metavar="RUN_DIR|latest", help="(re)judge a saved run, no chat")
    ap.add_argument("--remarks-only", metavar="RUN_DIR|latest", help="write remarks for a judged run, no chat/scores")
    ap.add_argument("--no-remarks", action="store_true", help="skip the written good/bad/improve remarks")
    args = ap.parse_args()

    bootstrap.setup()
    from evals.config import RESULTS_DIR, load_dataset
    from evals.tools_eval import Reference
    from evals.chat_eval import fill_expected, load_cases
    from evals.results import RunWriter

    ds = load_dataset(args.dataset)

    def saved_run(arg: str) -> Path:
        base = RESULTS_DIR / ds.name
        return sorted(base.glob("*_chat"))[-1] if arg == "latest" else Path(arg)

    def only(records):
        if not args.only:
            return records
        wanted = {x.strip() for x in args.only.split(",")}
        return {k: v for k, v in records.items() if k in wanted}

    if args.remarks_only:
        run_dir = saved_run(args.remarks_only)
        records = only(load_records(run_dir))
        scores = {t["name"]: t.get("judge") or {} for r in records.values() if not r.get("error") for t in r["turns"]}
        for r in records.values():
            if r.get("conversation_judge"):
                scores[r["id"]] = r["conversation_judge"]
        print(f"Writing remarks for saved run {run_dir.name} ({len(records)} case(s))...")
        cost = finish_remarks(ds, records, scores, run_dir)
        print(f"\nRemarks cost: ${cost:.2f}")
        return 0

    if args.judge_only:
        run_dir = saved_run(args.judge_only)
        records = only(load_records(run_dir))
        print(f"Judging saved run {run_dir.name} ({len(records)} case(s))...")
        scores, cost = run_judge(ds, records)
        if not args.no_remarks:
            cost += finish_remarks(ds, records, scores, run_dir, report_after=False)
        totals = report(records, scores, cost, run_dir)
        (run_dir / "summary.json").write_text(json.dumps(totals, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
        return 0

    cases = load_cases(ds)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        cases = [c for c in cases if c.id in wanted]
    fill_expected(cases, Reference(ds))
    if args.preview:
        preview(cases)
        review = ds.goldens_dir / "chat_review.md"
        write_review(cases, review)
        print(f"\n{len(cases)} case(s). Nothing was sent to the app.\nReview file: {review}")
        return 0

    writer = RunWriter(ds.name, "chat")
    run_chat(ds, cases, writer)
    records = load_records(writer.dir)
    scores, cost = ({}, 0.0)
    if not args.no_judge:
        print("\nJudging (Opus)...")
        try:
            scores, cost = run_judge(ds, records)
            if not args.no_remarks:
                cost += finish_remarks(ds, records, scores, writer.dir, report_after=False)
        except Exception as exc:
            print(f"Judge step failed: {exc}\nChat results are saved. Re-run judging with: --judge-only {writer.dir}")
    totals = report(records, scores, cost, writer.dir)
    writer.write_summary(totals)
    return 0


if __name__ == "__main__":
    sys.exit(main())
