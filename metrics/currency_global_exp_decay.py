from metric import Metric
from datahandler import DataHandler
import pandas as pd
from metrics.currency_global import CurrencyGlobal
from utils.memory_tracker import log_memory_snapshot
from utils.exponential_decay import exponential_decay, get_exp_factor

class CurrencyGlobalExp(Metric):
    """
    Calculates the global avarage of currency with exponetial decay shifting
    """
    metric_name = "currency_global_exp"

    def __init__(self, datahandler : DataHandler, currency_global: CurrencyGlobal, use_cache: bool):
        # super().__init__()
        self.datahandler = datahandler        
        self.currency_global = currency_global
        self.dependencies: list[Metric] = [self.currency_global]
        if use_cache:
            self.cache: float = self.datahandler.get_cache_float(self.metric_name)
        else:
            self.cache: float = -1
        self.exp_factor = get_exp_factor()
            
    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self) -> pd.DataFrame:
        pass

    def calculate(self):
        g: float = self.currency_global.get_last_value()
        if g == -1:
            self.cache = -1
            return
        self.cache = exponential_decay(g, self.exp_factor)

        self.write_result()
    
    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))

    def save_cache(self):
        self.datahandler.save_cache_float(self.metric_name, self.cache)