# not ready

"""Adjusted statement count for symmetric & inverse constrained properties.

For inverse properties the raw statement count is correct.
For symmetric properties, non-violating statements come in reciprocal pairs
(A,P,B) and (B,P,A) that represent a single relationship, so the raw count
double-counts them.  The adjusted count per symmetric property is:

    adjusted = (raw - violations) / 2 + violations

where *violations* is the number of statements missing their reciprocal
(each one is unique, not part of a pair).
"""

from __future__ import annotations

import pandas as pd

from metric import Metric
from datahandler import DataHandler
from statistics.constraint_violation_symmetric_inverse import (
    SymmetricInverseConstraintViolationCount,
)
from utils.memory_tracker import log_memory_snapshot

_DIFF_SQL = """
SELECT property_id,
  SUM(CASE WHEN "action" = 'CREATE' THEN 1 ELSE 0 END)
  - SUM(CASE WHEN "action" = 'DELETE' THEN 1 ELSE 0 END) AS diff_count
FROM value_change
WHERE property_id = ANY(?)
  AND target IN ('PROPERTY', 'PROPERTY_VALUE', 'ENTITY')
GROUP BY property_id
"""


class StatementCountPropertySymmInv(Metric):
    """Running total of statements for symmetric/inverse constrained properties.

    Symmetric property counts are adjusted so that reciprocal pairs are only
    counted once.  Inverse property counts are left as-is.
    """

    metric_name = "statement_count_property_sym_inv"

    def __init__(
        self,
        datahandler: DataHandler,
        use_cache: bool,
        sym_inv_metric: SymmetricInverseConstraintViolationCount,
    ):
        self.datahandler = datahandler
        self.sym_inv_metric = sym_inv_metric
        self.dependencies: list[Metric] = [sym_inv_metric]

        self.property_ids: list[int] = sym_inv_metric.all_watched
        self.inverse_pairs: dict[int, int] = sym_inv_metric.inverse_pairs

        # Per-property running raw statement counts.
        self.raw_counts: dict[int, int] = {}

        if use_cache:
            self.cache: float = self.datahandler.get_cache_float(self.metric_name)
            self._load_raw_counts_cache()
        else:
            self.cache: float = 0.0

    def _is_symmetric(self, prop: int) -> bool:
        return self.inverse_pairs.get(prop) == prop

    def calculate_diff(self) -> pd.DataFrame:
        if not self.property_ids:
            return pd.DataFrame(columns=["property_id", "diff_count"])

        df = self.datahandler.query_duckdb_df(_DIFF_SQL, [self.property_ids])
        return df if not df.empty else pd.DataFrame(columns=["property_id", "diff_count"])

    def calculate(self):
        diff_df = self.calculate_diff()

        for row in diff_df.itertuples(index=False):
            pid = int(row.property_id)
            self.raw_counts[pid] = self.raw_counts.get(pid, 0) + int(row.diff_count)

        violation_df = self.sym_inv_metric.get_last_value()
        if isinstance(violation_df, pd.DataFrame) and not violation_df.empty:
            violations = dict(
                zip(violation_df["property_id"].astype(int), violation_df["violation_count"])
            )
        else:
            violations = {}

        total = 0.0
        for prop, raw in self.raw_counts.items():
            v = violations.get(prop, 0)
            if self._is_symmetric(prop):
                total += (raw - v) / 2 + v
            else:
                total += raw

        self.cache = total
        self.write_result()

    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))

    def save_cache(self):
        self.datahandler.save_cache_float(self.metric_name, self.cache)
        df = pd.DataFrame(
            list(self.raw_counts.items()),
            columns=["property_id", "raw_count"],
        )
        self.datahandler.save_cache_df(f"{self.metric_name}_raw_counts", df)

    def _load_raw_counts_cache(self):
        df = self.datahandler.get_cache_df(f"{self.metric_name}_raw_counts")
        for pid, cnt in df.itertuples(index=False):
            self.raw_counts[int(pid)] = int(cnt)

    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)
