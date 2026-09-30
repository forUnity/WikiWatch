from metric import Metric
from datahandler import DataHandler, MetricOnPropertyValueFloat
import pandas as pd
from metrics.currency_per_property import CurrencyPerProperty
from utils.memory_tracker import log_memory_snapshot
from utils.exponential_decay import exponential_decay, get_exp_factor
import numpy as np

class CurrencyPerPropertyExp(Metric):
    """
    Calculates the currency per property with exponential decay shifting
    """
    metric_name = "currency_per_property_exp"

    def __init__(self, datahandler : DataHandler, currency_per_property: CurrencyPerProperty, use_cache: bool):
        # super().__init__()
        self.datahandler = datahandler        
        self.currency_per_property = currency_per_property
        self.dependencies: list[Metric] = [self.currency_per_property]
        if use_cache:
            self.cache: pd.DataFrame = self.datahandler.get_cache_df(self.metric_name)
        else:
            self.cache: pd.DataFrame = pd.DataFrame(columns=["property_id", "value"])
        self.exp_factor = get_exp_factor()
            
    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self) -> pd.DataFrame:
        pass

    def calculate(self):
        per_prop: pd.DataFrame = self.currency_per_property.get_last_value()
        if per_prop.empty:
            return
        
        self.cache = per_prop.apply(lambda x: exponential_decay(x, self.exp_factor))

        self.write_result()
    
    def write_result(self):
        series_writer = DataHandler.SeriesWriter(self.datahandler, self.metric_name, MetricOnPropertyValueFloat)
        for index, row in self.cache.iterrows():
            series_writer.add(index, float(row["value"]))
        series_writer.write_all()

    def save_cache(self):
        self.datahandler.save_cache_df(self.metric_name, self.cache.reset_index())