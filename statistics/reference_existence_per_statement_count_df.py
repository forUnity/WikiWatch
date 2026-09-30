from metric import Metric
from datahandler import DataHandler
import pandas as pd
from utils.memory_tracker import log_memory_snapshot

query = """
select value_id, 
    sum(case when "action" = 'CREATE' then 1 else -1 end) as diff_counts
from reference_change
where "change_target" = ''
group by value_id
"""

class ReferenceExistencePerStatementCountDf(Metric):
    """
    Running total of statements (property values) with at least one reference.
    TODO: Implement that functionality. for all statements: save (value_id, reference_count) in a dict in cache
    """
    metric_name = "reference_existence_per_statement_count_df"

    def __init__(self, datahandler : DataHandler, use_cache: bool):
        # super().__init__()
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []
        if use_cache:
            self.cache: int = self.datahandler.get_cache_int(self.metric_name)
            self.cache_df = self.datahandler.get_cache_df(self.metric_name)
        else:
            self.cache: int = 0
            self.cache_df = pd.DataFrame(columns=["value_id", "reference_count"])
        self.cache_df.set_index("value_id", inplace=True)
            
    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
            "cache_df": self.cache_df,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self) -> pd.DataFrame:
        diff_df = self.datahandler.query_duckdb_df(query)

        if diff_df.empty:
            return diff_df

        return diff_df.rename(columns={"diff_counts": "reference_count"})

    def calculate(self):
        diff_df = self.calculate_diff()
        diff_df.set_index("value_id", inplace=True)

        if diff_df.empty:
            self.write_result()
            return

        self.cache_df = self.cache_df.add(diff_df, fill_value=0)

        # Remove zero / negative references
        self.cache_df = self.cache_df[self.cache_df["reference_count"] > 0]

        # Update global count
        self.cache = len(self.cache_df)
        self.write_result()
    
    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))
    
    def save_cache(self):
        self.datahandler.save_cache_int(self.metric_name, self.cache)
        self.datahandler.save_cache_df(self.metric_name, self.cache_df.reset_index())
        