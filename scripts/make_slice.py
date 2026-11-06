"""Write the dataset's test file from its original, using the [source.slice] rule.

    uv run python scripts/make_slice.py --dataset tgspdcl_meter_readings

The original file is only read. If it is already under max_bytes and no slice
rule is set, it is copied as-is. Values are kept exactly as text (no type
conversion), so the test file matches the original byte-for-byte per cell.
"""
from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import duckdb  # noqa: E402

from evals.config import load_dataset  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset")
    ap.add_argument("--force", action="store_true", help="overwrite an existing test file")
    args = ap.parse_args()

    ds = load_dataset(args.dataset)
    src = ds.source
    if src.get("kind") != "file":
        print(f"Dataset '{ds.name}' is a {src.get('kind')} source; nothing to slice.")
        return 0

    original = Path(src["original_path"])
    target = Path(src["eval_path"])
    max_bytes = int(src.get("max_bytes", 0)) or None
    if target.resolve() == original.resolve():
        raise SystemExit("eval_path must differ from original_path (the original is never written).")
    if target.exists() and not args.force:
        raise SystemExit(f"{target} already exists. Use --force to rebuild it.")

    rule = ds.slice
    if not rule:
        if max_bytes and original.stat().st_size > max_bytes:
            raise SystemExit("Original is over max_bytes and no [source.slice] rule is set.")
        shutil.copyfile(original, target)
        print(f"Copied {original} -> {target}")
        return 0

    def lit(text: str) -> str:  # SQL string literal (DDL/COPY can't take parameters)
        return "'" + text.replace("'", "''") + "'"

    def ident(text: str) -> str:
        return '"' + text.replace('"', '""') + '"'

    con = duckdb.connect()
    con.execute(
        f"CREATE VIEW src AS SELECT * FROM read_csv({lit(original.as_posix())}, all_varchar = true, header = true)"
    )
    column = rule["column"]
    values = [str(v) for v in rule["values"]]
    in_list = ", ".join(lit(v) for v in values)
    con.execute(
        f"COPY (SELECT * FROM src WHERE {ident(column)} IN ({in_list})) "
        f"TO {lit(target.as_posix())} (HEADER, DELIMITER ',')"
    )
    total = con.execute("SELECT count(*) FROM src").fetchone()[0]
    kept = con.execute(
        "SELECT count(*) FROM read_csv(?, all_varchar = true, header = true)", [str(target)]
    ).fetchone()[0]
    size = target.stat().st_size
    print(f"Wrote {target}: {kept:,} of {total:,} rows ({column} in {values}), {size / 1e6:.1f} MB")
    if max_bytes and size > max_bytes:
        print(f"WARNING: test file is over max_bytes ({max_bytes / 1e6:.0f} MB). Narrow the slice.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
