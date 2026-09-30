from collections import defaultdict

import pandas as pd
from metric import Metric
from datahandler import DataHandler, MetricOnEntitySchemaAndPropertyValueFloat
from statistics.schema_fulfillment_count import SchemaFulfillmentCount
from utils.memory_tracker import log_memory_snapshot


class SchemaPropertyOccurrence(Metric):
    metric_name = "schema_property_occurrence"

    def __init__(self, datahandler: DataHandler, use_cache: bool, schema_fulfillment_count: SchemaFulfillmentCount):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = [schema_fulfillment_count]
        self.schema_fulfillment_count = schema_fulfillment_count
        if use_cache:
            self.cache: dict[tuple[int, int], float] = self.datahandler.get_cache_dict(self.metric_name)
        else:
            self.cache: dict[tuple[int, int], float] = {}
            
    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self) -> dict[tuple[int, int], float]:
        """
        For each (entity_schema, property) pair, compute the ratio of entities
        that have at least one statement for that property (compliant or not).

        Returns a dict mapping (entity_schema_id, property_id) -> ratio_has_property.
        """
        # dict[tuple[entity_id, property_id], tuple[num_compliant, num_noncompliant, min, max, entity_schema_id]]
        sfc_data: SchemaFulfillmentCount.DICT_TYPE = self.schema_fulfillment_count.get_last_value()
        if len(sfc_data) == 0:
            return {}
        
        # Group has_property flags by (entity_schema_id, property_id)
        groups: dict[tuple[int, int], list[bool]] = defaultdict(list)

        for (entity_schema_id, entity_id, property_id), (num_compliant, num_noncompliant, is_within_min_max) in sfc_data.items():
            if pd.isna(num_compliant) or pd.isna(num_noncompliant) or pd.isna(is_within_min_max):
                continue
            has_property = num_compliant > 0 or num_noncompliant > 0
            groups[(entity_schema_id, property_id)].append(has_property)

        return {key: sum(flags) / len(flags) for key, flags in groups.items()}

    def calculate(self):
        self.cache = self.calculate_diff()
        self.write_result()

    def write_result(self):
        series_writer = DataHandler.SeriesWriter(self.datahandler, self.metric_name, MetricOnEntitySchemaAndPropertyValueFloat)

        for (entity_schema_id, property_id), ratio in self.cache.items():
            series_writer.add(entity_schema_id, property_id, ratio)

        series_writer.write_all()

    def save_cache(self):
        self.datahandler.save_cache_dict(self.metric_name, self.cache, "entity_schema_id INT, property_id INT, ratio FLOAT, PRIMARY KEY (entity_schema_id, property_id)")
