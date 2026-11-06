"""Tools evaluation: test the MCP tools directly (no LLM, no judge cost).

    uv run python scripts/eval_tools.py --dataset tgspdcl_meter_readings --preview   # show cases + expected answers
    uv run python scripts/eval_tools.py --dataset tgspdcl_meter_readings             # run them
    uv run python scripts/eval_tools.py --dataset tgspdcl_meter_readings --only R01,S06

Cases: datasets/<dataset>/goldens/tools.yaml. Full results: results/<dataset>/<run>/.
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import bootstrap  # noqa: E402


def short(v, n=110) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str) if not isinstance(v, str) else v
    return s if len(s) <= n else s[: n - 3] + "..."


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--preview", action="store_true", help="show cases and expected answers; call nothing")
    ap.add_argument("--only", help="comma-separated case ids")
    args = ap.parse_args()

    bootstrap.setup()
    from evals.config import load_dataset
    from evals.tools_eval import ToolsRunner, load_cases
    from evals.results import RunWriter

    ds = load_dataset(args.dataset)
    cases = load_cases(ds)
    if args.only:
        wanted = {x.strip() for x in args.only.split(",")}
        cases = [c for c in cases if c.id in wanted]
    runner = ToolsRunner(ds, call=not args.preview)

    results = []
    area = None
    for case in cases:
        if case.area != area:
            area = case.area
            print(f"\n[{area}]")
        res = runner.run_case(case)
        results.append(res)
        if args.preview:
            sent = res.sent.get("arguments", {})
            q = (sent.get("queries") or [{}])[0] if "queries" in sent else None
            print(f"  {case.id}  {case.title}   [{case.expect}{' as ' + case.user if case.user != 'full_access' else ''}]")
            print(f"        sends: {res.sent.get('tool')} {short(q['sql'] if q else sent)}" if res.sent else f"        {res.detail}")
            if res.expected:
                print(f"        expected: {short(res.expected['rows'], 140)}")
        else:
            print(f"  {res.status:<5} {case.id}  {case.title}: {res.detail}")

    if args.preview:
        print(f"\n{len(results)} case(s). Nothing was sent to the app.")
        return 0

    writer = RunWriter(ds.name, "tools")
    for res in results:
        writer.write_case(res.id, res)
    counts = Counter(r.status for r in results)
    by_area: dict[str, Counter] = {}
    for r in results:
        by_area.setdefault(r.area, Counter())[r.status] += 1
    writer.write_summary({"counts": dict(counts), "by_area": {k: dict(v) for k, v in by_area.items()},
                          "failed": [r.id for r in results if r.status in ("FAIL", "ERROR")]})

    print("\nSummary by area")
    for a, c in by_area.items():
        print(f"  {a:<10} " + "  ".join(f"{k} {v}" for k, v in sorted(c.items())))
    print(f"\nTotal: {counts.get('PASS', 0)} passed, {counts.get('FAIL', 0)} failed, "
          f"{counts.get('ERROR', 0)} errors, {counts.get('SKIP', 0)} skipped")
    print(f"Full results: {writer.dir}")
    return 0 if not (counts.get("FAIL") or counts.get("ERROR")) else 1


if __name__ == "__main__":
    sys.exit(main())
