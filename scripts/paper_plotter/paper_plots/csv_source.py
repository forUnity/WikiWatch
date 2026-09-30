from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

import pandas as pd

from .model import DataKey, SeriesSpec


def load_csv_series(specs: Iterable[SeriesSpec]) -> dict[DataKey, pd.DataFrame]:
    """Load CSV-backed series, reading each file once.

    Every file needs a "timestamp" column; tz-naive timestamps are taken as
    UTC, like the database series. NaN values are kept so the line shows a gap
    where the value is unknown instead of interpolating across it.
    """
    columns_by_path: dict[str, set[str]] = defaultdict(set)
    for spec in specs:
        columns_by_path[spec.csv_path].add(spec.csv_column)

    result: dict[DataKey, pd.DataFrame] = {}
    for path, columns in columns_by_path.items():
        frame = pd.read_csv(path)
        missing = sorted(columns - set(frame.columns) - {"timestamp"})
        if "timestamp" not in frame.columns or missing:
            raise ValueError(
                f"{path} lacks column(s) {(['timestamp'] if 'timestamp' not in frame.columns else []) + missing}"
            )
        timestamps = pd.to_datetime(frame["timestamp"], utc=True)

        for column in columns:
            series = pd.DataFrame(
                {"timestamp": timestamps, "value": frame[column].astype(float)}
            ).sort_values("timestamp")
            if series["value"].isna().all():
                raise ValueError(f"No data in column {column!r} of {path}")
            result[("csv", path, column)] = series.reset_index(drop=True)

    return result
