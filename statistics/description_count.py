# numerator for property completeness

from __future__ import annotations

import pandas as pd
from metric import Metric
from utils.memory_tracker import log_memory_snapshot

_DELTAS_SQL = f"""
SELECT
  COALESCE(SUM(CASE WHEN action = 'CREATE' THEN 1 ELSE 0 END), 0) AS description_create_count,
  COALESCE(SUM(CASE WHEN action = 'DELETE' THEN 1 ELSE 0 END), 0) AS description_delete_count
FROM value_change
WHERE property_id = '-2';
"""


class DescriptionCount(Metric):
    """
    Per-entity count of descriptions.
    Running total of 'has value' contributions.
    """
    metric_name = "description_count"

    def __init__(self, datahandler, use_cache: bool):
        # super().__init__()
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []
        if use_cache:
            self.cache: float = self.datahandler.get_cache_float(self.metric_name)
        else:
            self.cache: float = 0.0
            
    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self) -> pd.DataFrame:
        df = self.datahandler.query_duckdb_df(_DELTAS_SQL)
        return df
    
    def calculate(self):
        df = self.calculate_diff()

        row = df.iloc[0]
        create_c = float(row["description_create_count"])
        delete_c = float(row["description_delete_count"])

        current = float(self.cache) if self.cache is not None else 0.0
        self.cache = current + create_c - delete_c
        self.write_result()

    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))
    
    def save_cache(self):
        self.datahandler.save_cache_float(self.metric_name, self.cache)
