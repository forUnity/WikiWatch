from __future__ import annotations

import logging
import time

import pandas as pd

from metric import Metric
from datahandler  import MetricOnPropertyValueFloat, DataHandler
from utils.property_constraints import (
    ConstraintRule,
    Side,
    compile_constraint_rules,
    constraint_type_to_property_ids,
    count_constraint_violation_diff_from_changes,
    get_wanted_property_constraints,
)
from utils.memory_tracker import log_memory_snapshot

CACHE_COLUMNS = ("property_id", "violation_count")

logger = logging.getLogger(__name__)


class PropertyConstraintViolationCount(Metric):
    """
    Generic per-property constraint violation counter.

    Customizable by subclass:
      - metric_name
      - wanted_constraint_types
      - deltas_sql
      - violates(rule,row,side)
      - (optional) calculate_diff() override for stateful constraints
    """
    metric_name: str
    wanted_constraint_types: set[str]
    deltas_sql: str

    def __init__(self, datahandler, use_cache: bool):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []
        if use_cache:
            self.cache: pd.DataFrame = self.datahandler.get_cache_df(self.metric_name)
        else:
            self.cache: pd.DataFrame = pd.DataFrame(columns=CACHE_COLUMNS)

        json_df = constraint_type_to_property_ids(self.wanted_constraint_types)
        json_property_ids = set(int(pid) for ids in json_df["property_ids"] for pid in ids)

        constraints_df = get_wanted_property_constraints(self.wanted_constraint_types)
        constraints_df = constraints_df[constraints_df["property_id"].isin(json_property_ids)]

        self.property_ids = sorted(json_property_ids)
        self.rules_by_property = compile_constraint_rules(constraints_df)

        self._last_series = (
            self.cache.set_index("property_id")["violation_count"]
            if not self.cache.empty
            else pd.Series(dtype="float64")
        )
            
    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
            "_last_series": self._last_series,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def violates(self, rule: ConstraintRule, row: pd.Series, side: Side) -> bool:
        raise NotImplementedError

    def calculate_diff(self) -> pd.DataFrame:
        """Default: stateless constraints via your existing helper."""
        if not self.property_ids:
            return pd.DataFrame(columns=["property_id", "violation_diff"])

        changes = self.datahandler.query_duckdb_df(self.deltas_sql, [self.property_ids])
        if changes.empty:
            return pd.DataFrame(columns=["property_id", "violation_diff"])

        counts = count_constraint_violation_diff_from_changes(
            changes,
            self.rules_by_property,
            violates_fn=self.violates,
        )
        return counts

    def calculate(self):
        df = self.calculate_diff()

        if df.empty:
            return self.write_result(self.cache)

        diff_series = df.set_index("property_id")["violation_diff"]

        self._last_series = self._last_series.add(diff_series, fill_value=0).clip(lower=0)

        self.cache = self._last_series.reset_index()
        self.cache.columns = ["property_id", "violation_count"]

        result_df = self._last_series.loc[diff_series.index].reset_index()
        result_df.columns = ["property_id", "violation_count"]

        self.write_result(result_df)

    def write_result(self, df: pd.DataFrame):
        series_writer = DataHandler.SeriesWriter(
            self.datahandler,
            self.metric_name,
            MetricOnPropertyValueFloat,
        )

        for row in df.itertuples(index=False):
            series_writer.add(int(row.property_id), float(row.violation_count))

        series_writer.write_all()

        

    def save_cache(self):
        self.datahandler.save_cache_df(self.metric_name, self.cache.reset_index(drop=True))