from __future__ import annotations

import logging
from typing import Any

import pandas as pd
from pympler import asizeof

import numpy as np
import os
import resource
import psutil

_PROCESS = psutil.Process(os.getpid())


def memusage():
    # Peak RSS (Linux: KB)
    mem_used = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return f"{mem_used / (1024**2):.2f} GiB"

def rss_memusage():
    # Current RSS (bytes)
    rss = _PROCESS.memory_info().rss
    return f"{rss / (1024**3):.2f} GiB"

logger = logging.getLogger(__name__)


def _safe_len(obj: Any) -> Any:
    try:
        if isinstance(obj, pd.DataFrame):
            return len(obj.index)
        return len(obj) if hasattr(obj, "__len__") else "N/A"
    except Exception:
        return "N/A"


def _size_bytes(obj: Any) -> int:
    """Return the estimated memory usage of an object in bytes."""
    if isinstance(obj, pd.DataFrame):
        return int(obj.memory_usage(deep=True).sum())
    return int(asizeof.asizeof(obj))


def log_memory_snapshot(label: str, tracked_attrs: dict[str, Any]) -> None:
    """Log the memory size and element count of each tracked structure."""

    if len(tracked_attrs) == 1:
        name, obj = next(iter(tracked_attrs.items()))
        count = _safe_len(obj)

        try:
            size = _size_bytes(obj)
            logger.info(
                "[%s] Memory snapshot — %s: %.2f MB (type %s, %s entries)",
                label,
                name,
                size / (1024 * 1024),
                type(obj),
                count,
            )
        except Exception as e:
            logger.warning(
                "[%s] Memory snapshot — %s: ERROR sizing object (type %s, %s entries): %s",
                label,
                name,
                type(obj),
                count,
                e,
            )
        return

    total = 0
    parts: list[str] = []

    for name, obj in tracked_attrs.items():
        count = _safe_len(obj)
        try:
            size = _size_bytes(obj)
            total += size
            parts.append(f"  {name}: {size / (1024 * 1024):.2f} MB ({count} entries)")
        except Exception as e:
            parts.append(f"  {name}: ERROR ({count} entries) [{e}]")

    lines = "\n".join(parts)
    logger.info(
        "[%s] Memory snapshot — total: %.2f MB\n%s",
        label,
        total / (1024 * 1024),
        lines,
    )