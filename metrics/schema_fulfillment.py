from typing import Iterable

import numpy as np
import pandas as pd
from metric import Metric
from datahandler import DataHandler
from statistics.schema_fulfillment_count import SchemaFulfillmentCount
from utils.memory_tracker import log_memory_snapshot

# Value of a schema_fulfillment_count row that does not exist (yet)
ABSENT_ROW = (0, 0, None)

# (schema, entity, property) key, row before the timestep, row after the timestep
RowChange = tuple[tuple[int, int, int], tuple, tuple]


def row_changes(schema_fulfillment_count: SchemaFulfillmentCount, full_pass: bool) -> Iterable[RowChange]:
    """The rows to apply: all of them on a full pass, otherwise the ones changed in this timestep."""
    sfc_data: SchemaFulfillmentCount.DICT_TYPE = schema_fulfillment_count.get_last_value()
    if full_pass:
        return ((key, ABSENT_ROW, row) for key, row in sfc_data.items())
    return ((key, old_row, sfc_data[key]) for key, old_row in schema_fulfillment_count.get_changed_rows().items())


class EntityFulfillmentTotals:
    """
    The number of properties and of complete properties per entity, and the average of the per-entity
    fractions over all entities that have at least one property.

    The per-entity counts live in arrays indexed by entity id, as there are tens of millions of entities.
    The sums are grouped by the number of properties of an entity, so that adding and removing
    contributions stays exact instead of accumulating float drift over the run.
    """

    def __init__(self):
        self.num_properties = np.zeros(0, dtype=np.uint16)
        self.num_complete = np.zeros(0, dtype=np.uint16)
        # number of properties t -> number of entities with t properties / sum of their complete properties
        self.entities_by_total: dict[int, int] = {}
        self.complete_by_total: dict[int, int] = {}

    def ensure_capacity(self, max_entity_id: int):
        if max_entity_id < len(self.num_properties):
            return
        size = max(max_entity_id + 1, 2 * len(self.num_properties))
        for name in ("num_properties", "num_complete"):
            grown = np.zeros(size, dtype=np.uint16)
            old = getattr(self, name)
            grown[:len(old)] = old
            setattr(self, name, grown)

    def update_entity(self, entity_id: int, properties_delta: int, complete_delta: int):
        """Moves the contribution of the entity to its new number of (complete) properties."""
        self.ensure_capacity(entity_id)
        old_total, old_complete = int(self.num_properties[entity_id]), int(self.num_complete[entity_id])
        new_total, new_complete = old_total + properties_delta, old_complete + complete_delta
        if old_total > 0:
            self.entities_by_total[old_total] -= 1
            self.complete_by_total[old_total] -= old_complete
        if new_total > 0:
            self.entities_by_total[new_total] = self.entities_by_total.get(new_total, 0) + 1
            self.complete_by_total[new_total] = self.complete_by_total.get(new_total, 0) + new_complete
        self.num_properties[entity_id] = new_total
        self.num_complete[entity_id] = new_complete

    def average(self) -> float | None:
        num_entities = sum(self.entities_by_total.values())
        if num_entities == 0:
            return None
        return sum(complete / total for total, complete in self.complete_by_total.items()) / num_entities


class SchemaFulfillment(Metric):
    """
    Average over all entities of the fraction of their schema properties that are complete.

    A property of an entity is complete if all its rows (one per entity schema of the entity that contains
    the property) are compliant. Entities only have rows once they are created (see SchemaFulfillmentCount),
    so the average is taken over the entities that exist at the end of the timestep.

    Maintained incrementally from the rows changed in each timestep.
    """
    metric_name = "schema_fulfillment"

    def __init__(self, datahandler: DataHandler, use_cache: bool, schema_fulfillment_count: SchemaFulfillmentCount):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = [schema_fulfillment_count]
        self.schema_fulfillment_count = schema_fulfillment_count
        if use_cache:
            self.cache: float = self.datahandler.get_cache_float(self.metric_name)
        else:
            self.cache: float = 0.0

        # Rebuilt from the whole schema_fulfillment_count cache on the first calculation
        self.totals = EntityFulfillmentTotals()
        self.initialized = False

    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
            "num_properties": self.totals.num_properties,
            "num_complete": self.totals.num_complete,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    @staticmethod
    def is_row_compliant(row: tuple) -> bool | None:
        """Whether the row is compliant, or None if it does not count (absent or legacy NA row)."""
        num_compliant, num_noncompliant, is_within_min_max = row
        if pd.isna(num_compliant) or pd.isna(num_noncompliant) or pd.isna(is_within_min_max):
            return None
        return bool(num_noncompliant == 0 and is_within_min_max)

    def property_state(self, state: bool | None, row: tuple) -> bool | None:
        """Combines the state of a property so far with one more of its rows: complete if all counting rows are compliant."""
        compliant = self.is_row_compliant(row)
        if compliant is None:
            return state
        if state is None:
            return compliant
        return state and compliant

    def calculate_diff(self) -> Iterable[RowChange]:
        """Returns the rows that changed since the last calculation, with their old and new value."""
        full_pass = not self.initialized  # covers a fresh run as well as a run resumed from cache
        self.initialized = True
        return row_changes(self.schema_fulfillment_count, full_pass)

    def apply_changes(self, changes: Iterable[RowChange]):
        # All rows of a property change together: an edit reaches every schema of the entity that contains
        # the property, and all rows of an entity are added on creation. So the old and new state of a
        # property can be computed from the changed rows alone.
        old_states: dict[tuple[int, int], bool | None] = {}
        new_states: dict[tuple[int, int], bool | None] = {}
        for (_, entity_id, property_id), old_row, new_row in changes:
            key = (entity_id, property_id)
            old_states[key] = self.property_state(old_states.get(key), old_row)
            new_states[key] = self.property_state(new_states.get(key), new_row)

        # entity_id -> (change in number of properties, change in number of complete properties)
        entity_deltas: dict[int, tuple[int, int]] = {}
        for key, old_state in old_states.items():
            new_state = new_states[key]
            if old_state == new_state:
                continue
            properties_delta = int(new_state is not None) - int(old_state is not None)
            complete_delta = int(bool(new_state)) - int(bool(old_state))
            d_properties, d_complete = entity_deltas.get(key[0], (0, 0))
            entity_deltas[key[0]] = (d_properties + properties_delta, d_complete + complete_delta)

        for entity_id, (properties_delta, complete_delta) in entity_deltas.items():
            self.totals.update_entity(entity_id, properties_delta, complete_delta)

    def calculate(self):
        self.apply_changes(self.calculate_diff())
        average = self.totals.average()
        if average is None:
            return
        self.cache = average
        self.write_result()

    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, self.cache)

    def save_cache(self):
        self.datahandler.save_cache_float(self.metric_name, self.cache)
