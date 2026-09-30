from datetime import datetime

import pandas as pd
from metric import Metric
from datahandler import DataHandler
from statistics.schema_fulfillment_count import SchemaFulfillmentCount
from utils.memory_tracker import log_memory_snapshot


def row_flags(row: tuple) -> tuple[bool, bool]:
    """
    Returns (is_failing, has_values) for one schema_fulfillment_count row.

    A row is failing if it holds a non-compliant value or its number of
    compliant values is outside the min/max of the schema. A row has values if
    the entity currently holds at least one value for that property.
    Rows with unknown counts (legacy NA rows) contribute to neither.
    """
    num_compliant, num_noncompliant, is_within_min_max = row
    if pd.isna(num_compliant) or pd.isna(num_noncompliant) or is_within_min_max is None or pd.isna(is_within_min_max):
        return False, False
    is_failing = num_noncompliant != 0 or not is_within_min_max
    has_values = num_compliant + num_noncompliant > 0
    return bool(is_failing), bool(has_values)


class TimeEntityComplete(Metric):
    metric_name = "time_entity_complete"

    def __init__(self, datahandler: DataHandler, use_cache: bool, schema_fulfillment_count: SchemaFulfillmentCount):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = [schema_fulfillment_count]
        self.schema_fulfillment_count = schema_fulfillment_count
        if use_cache:
            self.cache: dict[int, datetime] = self.datahandler.get_cache_dict(self.metric_name)
        else:
            self.cache: dict[int, datetime] = {}

        # entity_id -> number of failing rows / rows with values. Only nonzero
        # counts are stored.
        self.failing: dict[int, int] = {}
        self.valued: dict[int, int] = {}
        self.initialized = False

        # Entities that became complete or incomplete in the current timestep
        self.changed_entities: set[int] = set()

    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
            "failing": self.failing,
            "valued": self.valued,
            "changed_entities": self.changed_entities,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    @staticmethod
    def _add(counter: dict[int, int], entity_id: int, delta: int):
        if delta == 0:
            return
        count = counter.get(entity_id, 0) + delta
        if count == 0:
            del counter[entity_id]
        else:
            counter[entity_id] = count

    def calculate_diff(self) -> set[int]:
        """
        Updates the per-entity counters and returns the entities whose
        completeness may have changed.
        """
        if not self.initialized:
            # Full pass: covers a fresh run as well as a run resumed from cache
            self.initialized = True
            sfc_data: SchemaFulfillmentCount.DICT_TYPE = self.schema_fulfillment_count.get_last_value()
            dirty: set[int] = set(self.cache.keys())
            for (_, entity_id, _), row in sfc_data.items():
                is_failing, has_values = row_flags(row)
                if is_failing:
                    self._add(self.failing, entity_id, 1)
                if has_values:
                    self._add(self.valued, entity_id, 1)
                if is_failing or has_values:
                    dirty.add(entity_id)
            return dirty

        sfc_data = self.schema_fulfillment_count.get_last_value()
        dirty = set()
        for key, old_row in self.schema_fulfillment_count.get_changed_rows().items():
            entity_id = key[1]
            old_failing, old_valued = row_flags(old_row)
            new_failing, new_valued = row_flags(sfc_data[key])
            self._add(self.failing, entity_id, int(new_failing) - int(old_failing))
            self._add(self.valued, entity_id, int(new_valued) - int(old_valued))
            dirty.add(entity_id)
        return dirty

    def calculate(self):
        dirty = self.calculate_diff()
        timestamp = self.datahandler.current_end_timestep
        self.changed_entities = set()

        for entity_id in dirty:
            is_complete = entity_id in self.valued and entity_id not in self.failing
            if is_complete and entity_id not in self.cache:
                # First time this entity is fully complete
                self.cache[entity_id] = timestamp
                self.changed_entities.add(entity_id)
            elif not is_complete and entity_id in self.cache:
                # Entity is no longer complete, remove cached completion time
                del self.cache[entity_id]
                self.changed_entities.add(entity_id)

        self.write_result()

    def get_last_value(self) -> dict[int, datetime]:
        return self.cache

    def get_changed_entities(self) -> set[int]:
        return self.changed_entities

    def write_result(self):
        # Is not written to database
        pass

    def save_cache(self):
        self.datahandler.save_cache_dict(self.metric_name, self.cache, "entity_id INT, time_complete timestamp with time zone, primary key (entity_id)")
