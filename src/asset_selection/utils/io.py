"""Filesystem helpers used across modules."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Union

import pandas as pd


def ensure_dir(path: Union[str, Path]) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def write_json(path: Union[str, Path], data: Any, indent: int = 2) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=indent, default=_json_default)
    return p


def read_json(path: Union[str, Path]) -> Any:
    with Path(path).open("r", encoding="utf-8") as f:
        return json.load(f)


def write_csv(path: Union[str, Path], df: pd.DataFrame) -> Path:
    p = Path(path)
    ensure_dir(p.parent)
    df.to_csv(p, index=False)
    return p


def _json_default(obj: Any) -> Any:
    # pandas/numpy types are not JSON-serializable by default.
    try:
        import numpy as np

        if isinstance(obj, np.generic):
            return obj.item()
        if isinstance(obj, np.ndarray):
            return obj.tolist()
    except Exception:
        pass
    if isinstance(obj, pd.Timestamp):
        return obj.isoformat()
    if isinstance(obj, set):
        return sorted(obj)
    if hasattr(obj, "isoformat"):
        return obj.isoformat()
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")
