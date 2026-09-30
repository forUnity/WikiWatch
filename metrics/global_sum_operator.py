from __future__ import annotations

import pandas as pd

from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot


class GlobalSumOperator(Metric):
    """
    Sums one or more source metrics into a single scalar.
    Each source can be a DataFrame (summed via *value_column*) or a scalar.
    """

    def __init__(
        self,
        datahandler: DataHandler,
        use_cache: bool,
        metric_name: str,
        source_metrics: Metric | list[Metric],
        value_column: str | None = None,
    ):
        self.metric_name: str = metric_name
        self.datahandler: DataHandler = datahandler
        self.source_metrics: list[Metric] = (
            source_metrics if isinstance(source_metrics, list) else [source_metrics]
        )
        self.value_column: str | None = value_column
        self.dependencies: list[Metric] = list(self.source_metrics)
        if use_cache:
            self.cache: float = self.datahandler.get_cache_float(self.metric_name)
        else:
            self.cache: float = 0.0
            
    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self):
        # Not needed for this derived metric
        pass

    def calculate(self):
        total = 0.0
        for src in self.source_metrics:
            value = src.get_last_value()
            if isinstance(value, pd.DataFrame):
                if not value.empty and self.value_column is not None:
                    total += float(value[self.value_column].sum())
            else:
                total += float(value) if value is not None else 0.0
        self.cache = total
        self.write_result()

    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, self.cache)

    def save_cache(self):
        self.datahandler.save_cache_float(self.metric_name, self.cache)
