"""Complement operator: outputs 1 - source_value.

Useful for converting a violation ratio into a completeness ratio.
"""

from __future__ import annotations

from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot


class GlobalComplementOperator(Metric):
    """Value = 1.0 - source metric value."""

    def __init__(
        self,
        datahandler: DataHandler,
        metric_name: str,
        source_metric: Metric,
        use_cache: bool,
    ):
        self.metric_name: str = metric_name
        self.datahandler: DataHandler = datahandler
        self.source_metric: Metric = source_metric
        self.dependencies: list[Metric] = [self.source_metric]
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
        pass

    def calculate(self):
        val = self.source_metric.get_last_value()
        self.cache = 1.0 - (float(val) if val is not None else 0.0)
        self.write_result()

    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))

    def save_cache(self):
        self.datahandler.save_cache_float(self.metric_name, self.cache)
