from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

query = """
select (select count(*) 
from value_change 
where 
    target in ('PROPERTY', 'ENTITY')
    and "action" = 'CREATE'
) - (select count(*) 
from value_change 
where 
    target = 'PROPERTY'
    and "action" = 'DELETE'
) as diff_count
"""
# NOTE: We don't have record of deleted entities. Otherwise WHERE action = 'DELETED' AND target = 'ENTITY'should also be substracted.

class PropertyCount(Metric):
    """
    Running total of statements.
    """
    metric_name = "property_count"

    def __init__(self, datahandler: DataHandler, use_cache: bool):
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
    