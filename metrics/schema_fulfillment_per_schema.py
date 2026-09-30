from typing import Iterable

from metric import Metric
from datahandler import DataHandler, MetricOnEntitySchemaValueFloat
from metrics.schema_fulfillment import EntityFulfillmentTotals, RowChange, SchemaFulfillment, row_changes
from statistics.schema_fulfillment_count import SchemaFulfillmentCount
from utils.memory_tracker import log_memory_snapshot


class SchemaFulfillmentPerSchema(Metric):
    """
    Schema fulfillment per entity schema: for each schema, the average over the entities belonging to it of
    the fraction of the properties of that schema that are complete.

    A row is judged exactly as in SchemaFulfillment, but only the rows of the schema in question count, so
    an entity belonging to several schemas is scored separately for each of them. Entities only have rows
    once they are created and every property of the schema counts, whether it was ever edited or not (see
    SchemaFulfillmentCount).

    Maintained incrementally, with the per-entity counts of one EntityFulfillmentTotals per schema. Those
    counts are arrays over the entity ids, so this metric is meant for runs restricted to a few schemas
    (see the entity_schema_ids parameter of SchemaFulfillmentCount).
    """
    metric_name = "schema_fulfillment_per_schema"

    # Rows are judged as in the global metric, so both stay in step
    is_row_compliant = staticmethod(SchemaFulfillment.is_row_compliant)

    def __init__(self, datahandler: DataHandler, use_cache: bool, schema_fulfillment_count: SchemaFulfillmentCount):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = [schema_fulfillment_count]
        self.schema_fulfillment_count = schema_fulfillment_count
        if use_cache:
            self.cache: dict[int, float] = self.datahandler.get_cache_dict(self.metric_name)
        else:
            self.cache: dict[int, float] = {}

        # entity_schema_id -> per-entity counts, rebuilt on the first calculation
        self.totals: dict[int, EntityFulfillmentTotals] = {}
        self.initialized = False

    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
            "totals": self.totals,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self) -> Iterable[RowChange]:
        """Returns the rows that changed since the last calculation, with their old and new value."""
        full_pass = not self.initialized  # covers a fresh run as well as a run resumed from cache
        self.initialized = True
        return row_changes(self.schema_fulfillment_count, full_pass)

    def apply_changes(self, changes: Iterable[RowChange]):
        # (schema, entity) -> (change in number of properties, change in number of complete properties).
        # A property has exactly one row per schema, so no combining across rows is needed here.
        deltas: dict[tuple[int, int], tuple[int, int]] = {}
        for (entity_schema_id, entity_id, _), old_row, new_row in changes:
            old_state = self.is_row_compliant(old_row)
            new_state = self.is_row_compliant(new_row)
            if old_state == new_state:
                continue
            properties_delta = int(new_state is not None) - int(old_state is not None)
            complete_delta = int(bool(new_state)) - int(bool(old_state))
            key = (entity_schema_id, entity_id)
            d_properties, d_complete = deltas.get(key, (0, 0))
            deltas[key] = (d_properties + properties_delta, d_complete + complete_delta)

        for (entity_schema_id, entity_id), (properties_delta, complete_delta) in deltas.items():
            totals = self.totals.get(entity_schema_id)
            if totals is None:
                totals = self.totals[entity_schema_id] = EntityFulfillmentTotals()
            totals.update_entity(entity_id, properties_delta, complete_delta)

    def calculate(self):
        self.apply_changes(self.calculate_diff())
        self.cache = {
            entity_schema_id: average
            for entity_schema_id, totals in self.totals.items()
            if (average := totals.average()) is not None
        }
        self.write_result()

    def write_result(self):
        series_writer = DataHandler.SeriesWriter(self.datahandler, self.metric_name, MetricOnEntitySchemaValueFloat)

        for entity_schema_id, score in self.cache.items():
            series_writer.add(entity_schema_id, score)

        series_writer.write_all()

    def save_cache(self):
        self.datahandler.save_cache_dict(self.metric_name, self.cache, "entity_schema_id INT PRIMARY KEY, score FLOAT")
