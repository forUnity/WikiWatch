from __future__ import annotations

from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot


class GlobalDivisionOperator(Metric):
    """
    Value = Numerator / Denominator   (1 if Denominator == 0)

    Both numerator and denominator metrics must provide scalar values
    via get_last_value().  Use GlobalSumOperator to aggregate per-property
    DataFrames into a scalar before feeding them into this operator.
    """

    def __init__(
        self,
        datahandler: DataHandler,
        metric_name: str,
        numerator_metric: Metric,
        denominator_metric: Metric,
        use_cache: bool,
    ):
        # super().__init__()
        self.metric_name: str = metric_name
    
        self.datahandler: DataHandler = datahandler
        self.numerator_metric: Metric = numerator_metric
        self.denominator_metric: Metric = denominator_metric
       
        self.dependencies: list[Metric] = [self.numerator_metric, self.denominator_metric]
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
        num = self.numerator_metric.get_last_value()
        den = self.denominator_metric.get_last_value()

        if den == 0:
            return
        else:        
            self.cache = num / den
        self.write_result()

    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))

    def save_cache(self):
        self.datahandler.save_cache_float(self.metric_name, self.cache)