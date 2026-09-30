from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

query = """
select (select count(*) 
from reference_change
where "action" = 'CREATE'
) - (select count(*) 
from reference_change 
where "action" = 'DELETE'
) as diff_count
"""

class ReferenceCount(Metric):
    """
    Running total of references. #NOTE: Count is not accurate because multiple fields of the same reference are counted as seperate references.
    #TODO: Should be adjusted if we want to use this metric somehow (currently not planned).
    """
    metric_name = "reference_count"

    def __init__(self, datahandler : DataHandler, use_cache: bool):
        # super().__init__()
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []
        if use_cache:
            self.cache: int = self.datahandler.get_cache_int(self.metric_name)
        else:
            self.cache: int = 0
            
    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self):
        query_result = self.datahandler.query_duckdb_df(query)
        return query_result['diff_count'].iloc[0] if not query_result.empty else 0

    def calculate(self):
        diff_value = self.calculate_diff()
        self.cache += diff_value
        self.write_result()
    
    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))
    
    def save_cache(self):
        self.datahandler.save_cache_int(self.metric_name, self.cache)
