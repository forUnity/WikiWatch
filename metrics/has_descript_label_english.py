from __future__ import annotations

from pandas import DataFrame
from metric import Metric
from datahandler import DataHandler
from statistics.entity_count import EntityCount
from statistics.label_count import LabelCount
from statistics.description_count import DescriptionCount
from utils.memory_tracker import log_memory_snapshot

import numpy as np


class HasDescriptlabel(Metric):
    """
    This is spicifically for the english language. 
    Has_descript_label = #E_label + #E_descript / 2 * #E   (1 if denominator_p == 0)
    """
    metric_name = "Has_descript_label"

    def __init__(
        self,
        datahandler: DataHandler,
        label_count : LabelCount,
        desciption_count: DescriptionCount,
        entity_count: EntityCount,
        use_cache: bool,
    ):
        # super().__init__()
        self.datahandler: DataHandler = datahandler
        self.label_count: LabelCount = label_count
        self.desciption_count: DescriptionCount = desciption_count
        self.entity_count: EntityCount = entity_count
       
        self.dependencies: list[Metric] = [self.desciption_count, self.label_count, self.entity_count]
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
        # Not needed for this derived metric
        pass

    def calculate(self):
        descriptions = self.desciption_count.get_last_value()
        labels = self.label_count.get_last_value()
        entity_count = self.entity_count.get_last_value()
        
        denom = 2.0 * entity_count
        if denom == 0:
            value = 1.0
        else:
            value = (labels + descriptions) / denom

        self.cache = value
        self.write_result()

    def write_result(self):
        # TODO: optimize via batch write if supported
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))
    
    def save_cache(self):
        self.datahandler.save_cache_int(self.metric_name, self.cache)
