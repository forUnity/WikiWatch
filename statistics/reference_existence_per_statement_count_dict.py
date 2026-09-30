from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

query = """
select value_id, 
    sum(case when "action" = 'CREATE' then 1 else -1 end) as diff_counts
from reference_change
where "change_target" = ''
group by value_id
"""

class ReferenceExistencePerStatementCountDict(Metric):
    """
    Running total of statements (property values) with at least one reference.
    TODO: Implement that functionality. for all statements: save (value_id, reference_count) in a dict in cache
    """
    metric_name = "reference_existence_per_statement_count_dict"
    cache_df: dict[str, int]

    def __init__(self, datahandler : DataHandler, use_cache: bool):
        # super().__init__()
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []
        if use_cache:
            self.cache: int = self.datahandler.get_cache_int(self.metric_name)
            self.cache_dict = self.datahandler.get_cache_dict(self.metric_name)  # key: value_id, value: reference_count, all statements that currently have at least one reference
        else:
            self.cache: int = 0
            self.cache_dict = {}
            
    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
            "cache_dict": self.cache_dict,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self):
        query_result = self.datahandler.query_duckdb_df(query) # diff dict to apply to cached dict
        return query_result.set_index("value_id")["diff_counts"].to_dict() if not query_result.empty else {}

    def calculate(self):
        diff_dict = self.calculate_diff()
        #diff_dict = diff_df.to_dict()
        diff_value = 0
        
        for key, diff in diff_dict.items():
            old = self.cache_dict.get(key, 0)
            new = old + diff

            # became present
            if old == 0 and diff > 0:
                diff_value += 1
            # became absent
            elif new == 0 and diff < 0:
                diff_value -= 1

            if new:
                self.cache_dict[key] = int(new)
            else:
                self.cache_dict.pop(key, None)

        # update global count
        self.cache += diff_value
        self.write_result()
    
    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))
    
    def save_cache(self):
        self.datahandler.save_cache_int(self.metric_name, self.cache)
        self.datahandler.save_cache_dict(self.metric_name, self.cache_dict, "value_id VARCHAR(100), diff_counts INT")
        