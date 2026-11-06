"""Confirm a dataset's test setup is complete before running evals. Read-only.

    uv run python scripts/check_setup.py --dataset tgspdcl_meter_readings [--judge]

--judge also sends one small request to the judge model (costs about $0.01).
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from evals import bootstrap  # noqa: E402
from evals.config import load_dataset  # noqa: E402

results: list[tuple[bool, str]] = []


def report(ok: bool, text: str) -> bool:
    results.append((ok, text))
    print(("  OK   " if ok else "  FAIL ") + text)
    return ok


def info(text: str) -> None:
    print("  --   " + text)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--judge", action="store_true")
    args = ap.parse_args()

    bootstrap.setup()
    ds = load_dataset(args.dataset)
    print(f"\nSetup check: dataset '{ds.name}'\n")

    # --- test file -------------------------------------------------------------------
    print("Test data")
    header: list[str] = []
    if ds.reference_file:
        path = Path(ds.reference_file)
        label = "test file" if ds.source.get("kind") == "file" else "reference CSV (answer key)"
        if report(path.exists(), f"{label} exists: {path}"):
            size = path.stat().st_size
            limit = int(ds.source.get("max_bytes") or 0) if ds.source.get("kind") == "file" else 0
            if limit:
                report(size <= limit, f"test file size {size / 1e6:.1f} MB (limit {limit / 1e6:.0f} MB)")
            import csv

            with path.open(encoding="utf-8", errors="replace", newline="") as fh:
                header = [h.strip() for h in next(csv.reader(fh))]

    # --- tenant / source -------------------------------------------------------------
    from catalog.models import DataSource, SchemaContext, SourceColumn, SourceObject
    from tenancy.models import Tenant, TenantMembership

    print("\nTenant and source")
    tenant = Tenant.objects.filter(slug=ds.tenant_slug).first()
    if not report(bool(tenant and tenant.is_active), f"tenant '{ds.tenant_slug}' exists and is active"):
        return summary()
    source = DataSource.objects.filter(tenant=tenant, source_id=ds.source_id).first()
    if not report(bool(source and source.enabled), f"data source '{ds.source_id}' exists and is enabled"):
        return summary()

    if ds.source.get("kind") == "file":
        fv = source.file_versions.filter(is_active=True).first()
        if report(fv is not None, "source has an active file version"):
            expected = Path(ds.source["eval_path"]).name
            report(fv.original_filename == expected,
                   f"active file is '{fv.original_filename}' (expected '{expected}')")

    objs = SourceObject.objects.filter(source=source)
    report(objs.exists(), f"source objects: {objs.count()}")
    cols = SourceColumn.objects.filter(source_object__in=objs)
    col_names = set(cols.values_list("column_name", flat=True))
    if header:
        missing = [h for h in header if h not in col_names]
        report(not missing, f"all {len(header)} file columns are in the catalog"
               + (f" (missing {len(missing)}: {missing[:5]})" if missing else ""))
    report(not objs.filter(anonymized_name__isnull=True).exists()
           and not cols.filter(anonymized_name__isnull=True).exists(),
           "every object and column has an anonymized name")

    ctx = SchemaContext.objects.filter(source=source).order_by("-version").first()
    report(bool(ctx and ctx.status == SchemaContext.STATUS_APPROVED),
           f"schema context approved (latest: {ctx.status if ctx else 'none'})")

    # --- sensitivity -----------------------------------------------------------------
    print("\nSensitive columns")
    by_name = {c.column_name: c for c in cols}
    for col, expected_class in ds.sensitive_columns.items():
        c = by_name.get(col)
        actual = c.sensitivity_classification if c else None
        report(actual == expected_class, f"{col}: {actual or 'not found'} (expected {expected_class})")

    # --- users -----------------------------------------------------------------------
    print("\nTest users")
    from accounts.models import User

    for label, tu in ds.users.items():
        user = User.objects.filter(email=tu.email).first()
        if not report(bool(user and user.is_active), f"[{label}] user {tu.email} exists and is active"):
            continue
        m = TenantMembership.objects.filter(user=user, tenant=tenant, is_active=True).first()
        if not report(m is not None, f"[{label}] is an active member of '{ds.tenant_slug}'"):
            continue
        roles = set(m.membership_roles.values_list("role__code", flat=True))
        report(tu.role in roles, f"[{label}] holds role '{tu.role}' (has: {sorted(roles) or 'none'})")

    # --- runtime ---------------------------------------------------------------------
    print("\nRuntime")
    report(User.objects.filter(email="_mcp_discovery_service@system.local", is_active=True).exists(),
           "MCP discovery service account exists")
    import httpx

    from conversation.mcp.config import get_mcp_server_urls

    for url in get_mcp_server_urls():
        health = url.rsplit("/mcp", 1)[0] + "/healthz"
        try:
            ok = httpx.get(health, timeout=5).status_code == 200
        except Exception:
            ok = False
        report(ok, f"MCP server reachable: {health}")

    chat_model = os.getenv("ANTHROPIC_MODEL_NAME", "claude-sonnet-5-5")
    schema_model = os.getenv("SCHEMA_CONTEXT_MODEL", "claude-haiku-5-5")
    judge_model = os.getenv("JUDGE_MODEL", "claude-opus-5-5")
    info(f"models: chat={chat_model}  schema-context={schema_model}  judge={judge_model}")
    report(judge_model not in {chat_model, schema_model}, "judge model differs from the models under test")
    report(bool(os.getenv("ANTHROPIC_API_KEY")), "ANTHROPIC_API_KEY is set")
    if os.getenv("CONFIDENT_API_KEY"):
        info("Confident AI key set: masked results will upload to the dashboard")
    else:
        info("Confident AI key not set: results stay local only")

    if args.judge:
        from evals.judge import get_judge

        try:
            reply = get_judge().generate("Reply with the single word: ready")
            text = reply[0] if isinstance(reply, tuple) else reply
            report("ready" in str(text).lower(), f"judge responds ({judge_model})")
        except Exception as exc:
            report(False, f"judge call failed: {exc}")

    return summary()


def summary() -> int:
    failed = [t for ok, t in results if not ok]
    print(f"\n{len(results) - len(failed)} passed, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
