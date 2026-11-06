"""Delete chat sessions created by eval runs in a dataset's test tenant.

    uv run python scripts/cleanup.py --dataset tgspdcl_meter_readings          # list only
    uv run python scripts/cleanup.py --dataset tgspdcl_meter_readings --delete # delete

Only sessions in the dataset's test tenant whose title starts with "[eval] " are touched
(their messages, tool calls and summaries go with them via the app's cascades).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import bootstrap  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--delete", action="store_true")
    args = ap.parse_args()

    bootstrap.setup()
    from conversation.models import Session

    from evals.chat_runner import EVAL_SESSION_PREFIX
    from evals.config import load_dataset

    ds = load_dataset(args.dataset)
    qs = Session.objects.filter(tenant__slug=ds.tenant_slug, title__startswith=EVAL_SESSION_PREFIX)
    print(f"{qs.count()} eval session(s) in tenant '{ds.tenant_slug}'")
    for s in qs.order_by("-created_at")[:20]:
        print(f"  #{s.id}  {s.created_at:%Y-%m-%d %H:%M}  {s.title}")
    if args.delete:
        deleted, by_model = qs.delete()
        print(f"Deleted {deleted} row(s): {by_model}")
    else:
        print("List only. Add --delete to remove them.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
