"""Paths and per-dataset configuration. Nothing here is specific to one dataset."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

EVALS_DIR = Path(__file__).resolve().parents[1]
APP_DIR = EVALS_DIR.parent / "mcp-utilities"
DATASETS_DIR = EVALS_DIR / "datasets"
RESULTS_DIR = EVALS_DIR / "results"


@dataclass
class TestUser:
    label: str
    email: str
    role: str
    can_query: bool


@dataclass
class Dataset:
    name: str
    description: str
    folder: Path
    source: dict[str, Any]
    tenant_slug: str
    source_id: str
    users: dict[str, TestUser]
    sensitive_columns: dict[str, str]
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def goldens_dir(self) -> Path:
        return self.folder / "goldens"

    @property
    def slice(self) -> dict[str, Any] | None:
        return self.source.get("slice")

    @property
    def reference_file(self) -> str | None:
        """CSV holding the same data the app queries: used for expected answers and sensitive values."""
        if self.source.get("kind") == "file":
            return self.source.get("eval_path")
        return self.source.get("reference_path")

    def user(self, label: str) -> TestUser:
        return self.users[label]


def available_datasets() -> list[str]:
    return sorted(p.parent.name for p in DATASETS_DIR.glob("*/dataset.toml"))


def load_dataset(name: str | None = None) -> Dataset:
    """Load a dataset by name, or from EVAL_DATASET when no name is given."""
    name = name or os.getenv("EVAL_DATASET")
    if not name:
        raise SystemExit(f"No dataset given. Use --dataset or EVAL_DATASET. Available: {available_datasets()}")
    folder = DATASETS_DIR / name
    path = folder / "dataset.toml"
    if not path.exists():
        raise SystemExit(f"Dataset '{name}' not found. Available: {available_datasets()}")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    users = {
        label: TestUser(label=label, email=u["email"], role=u["role"], can_query=bool(u.get("can_query", True)))
        for label, u in raw.get("users", {}).items()
    }
    return Dataset(
        name=raw.get("name", name),
        description=raw.get("description", ""),
        folder=folder,
        source=raw["source"],
        tenant_slug=raw["tenant"]["slug"],
        source_id=raw["tenant"]["source_id"],
        users=users,
        sensitive_columns=dict(raw.get("sensitive_columns", {})),
        raw=raw,
    )
