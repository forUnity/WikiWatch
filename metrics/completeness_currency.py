"""

Outputs, globally and per class (all schema-mapped classes of the entity):
    completeness_currency_avg_age_years[_per_class]
        average completion age in years
    completeness_currency_avg_score[_per_class]
        average of exp(-a * completion_age) with a = -ln(0.5) / 1 year, so a
        delay of one year scores 0.5
    completeness_currency_entity_count[_per_class]
        number of entities the averages are taken over
"""

import math
from dataclasses import dataclass

import pandas as pd
from metric import Metric
from datahandler import DataHandler, MetricOnClassValueFloat
from statistics.time_entity_complete import TimeEntityComplete
from utils.exponential_decay import exponential_decay, get_exp_factor
from utils.memory_tracker import log_memory_snapshot
from utils.time_values import CUTOFFDATE, parse_time_value

# Same year length as get_exp_factor, so a score of 0.5 is an age of 1.0 years
SECONDS_PER_YEAR = 365 * 24 * 60 * 60

# Properties that define time_happened, from most to least favorable
TIME_HAPPENED_PROPERTIES = [
    'publication date',
    'year of publication of scientific name for taxon',
    'dissolved, abolished or demolished date',
    'date of death',
    'date of birth',
    'time of discovery or invention',
    'end time',
    'point in time',
    'inception',
    'start time',
]

_rank_cases = "\n".join(
    f"                WHEN '{label}' THEN {rank}"
    for rank, label in enumerate(TIME_HAPPENED_PROPERTIES, start=1)
)
_labels = ", ".join(f"'{label}'" for label in TIME_HAPPENED_PROPERTIES)

# Returns for each entity schema the most favorable time happened property
# occurring in it, together with its rank
es_time_happend_property_subquery = f"""
SELECT entity_schema_id, property_id AS time_happened_property_id, property_rank
FROM (
    SELECT
        entity_schema_id,
        property_id,
        property_rank,
        ROW_NUMBER() OVER (
            PARTITION BY entity_schema_id
            ORDER BY property_rank
        ) AS rn
    FROM (
        SELECT
            es.entity_schema_id,
            es.property_id,
            CASE pl.label
{_rank_cases}
            END AS property_rank
        FROM postgres_db.entity_schemas es
        INNER JOIN postgres_db.property_labels pl
            ON es.property_id = pl.property_id
        WHERE pl.label IN ({_labels})
    ) ranked
) t
WHERE rn = 1
"""

# Returns every change of the current timestep to a time value of an entity,
# with the rank of the property for the entity (NULL if the property is not a
# time happened property of any of its schemas) and all schema-mapped classes
# of the entity. Parsing and the CUTOFFDATE scope check are left to Python, so
# that this metric and the data age at insertion metric share one
# implementation of both (utils/time_values.py).
time_happened_changes_query = f"""
SELECT
    value_change.timestamp,
    value_change.value_id,
    value_change.entity_id,
    value_change.action,
    value_change.new_datatype,
    value_change.new_value,
    MIN(es_time_happened_property.property_rank) AS property_rank,
    list(DISTINCT schema_class_mapping.class_id) AS class_ids
FROM value_change
INNER JOIN postgres_db.entity_type ON value_change.entity_id = entity_type.entity_id
INNER JOIN postgres_db.schema_class_mapping ON schema_class_mapping.class_id = entity_type.class_id
LEFT JOIN ({es_time_happend_property_subquery}) AS es_time_happened_property
    ON es_time_happened_property.entity_schema_id = schema_class_mapping.entity_schema_id
    AND es_time_happened_property.time_happened_property_id = value_change.property_id
WHERE value_change.old_datatype = 'time' OR value_change.new_datatype = 'time'
GROUP BY
    value_change.timestamp,
    value_change.value_id,
    value_change.entity_id,
    value_change.action,
    value_change.new_datatype,
    value_change.new_value
HAVING MIN(es_time_happened_property.property_rank) IS NOT NULL
ORDER BY value_change.timestamp ASC, value_change.value_id ASC
"""


@dataclass
class Totals:
    count: int = 0
    age_sum: int = 0
    score_sum: float = 0.0

    def add(self, age: int, score: float):
        self.count += 1
        self.age_sum += age
        self.score_sum += score

    def remove(self, age: int, score: float):
        self.count -= 1
        self.age_sum -= age
        self.score_sum -= score
        if self.count == 0:
            # Avoid carrying float drift into a later, unrelated contribution
            self.score_sum = 0.0

    def avg_age_years(self) -> float:
        return self.age_sum / self.count / SECONDS_PER_YEAR

    def avg_score(self) -> float:
        return self.score_sum / self.count


class CompletenessCurrency(Metric):
    metric_name = "completeness_currency"

    def __init__(self, datahandler: DataHandler, use_cache: bool, time_entity_complete: TimeEntityComplete):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = [time_entity_complete]
        self.time_entity_complete = time_entity_complete
        self.exp_factor = get_exp_factor()

        # entity_id -> ((value_id, property_rank, time_happened in epoch seconds), ...)
        # In-scope time values only. value_change only holds the current
        # timestep, so the values have to be carried across timesteps.
        self.happened: dict[int, tuple[tuple[str, int, int], ...]] = {}
        # entity_id -> schema-mapped class ids, for entities in happened
        self.classes: dict[int, tuple[int, ...]] = {}
        if use_cache:
            self._load_cache()

        # entity_id -> (completion age in seconds, score, class ids) for the
        # entities currently included in the aggregates. Rebuilt on the first
        # calculation.
        self.contributions: dict[int, tuple[int, float, tuple[int, ...]]] = {}
        self.global_totals = Totals()
        self.class_totals: dict[int, Totals] = {}
        self.initialized = False

    def log_memory(self):
        tracked_attrs = {
            "happened": self.happened,
            "classes": self.classes,
            "contributions": self.contributions,
            "class_totals": self.class_totals,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def update_happened(self) -> set[int]:
        """
        Applies the time value changes of the current timestep to the state.
        Returns the entities whose time_happened may have changed.
        """
        changes: pd.DataFrame = self.datahandler.query_duckdb_df(time_happened_changes_query)
        changed_entities: set[int] = set()
        dropped_precision = 0
        dropped_cutoff = 0
        cutoff = int(CUTOFFDATE.timestamp())

        for row in changes.itertuples(index=False):
            entity_id = int(row.entity_id)
            value_id = str(row.value_id)
            changed_entities.add(entity_id)

            value_time = None
            if row.action != "DELETE" and row.new_datatype == "time":
                parsed = parse_time_value(row.new_value)
                if parsed is None:
                    dropped_precision += 1
                elif int(parsed.timestamp()) < cutoff:
                    dropped_cutoff += 1
                else:
                    value_time = int(parsed.timestamp())
            # A pre-cutoff or unusable value replaces the previous value of the
            # statement, so the statement drops out of the state

            values = tuple(v for v in self.happened.get(entity_id, ()) if v[0] != value_id)
            if value_time is not None:
                values += ((value_id, int(row.property_rank), value_time),)

            if values:
                self.happened[entity_id] = values
                self.classes[entity_id] = tuple(sorted(int(c) for c in row.class_ids))
            else:
                self.happened.pop(entity_id, None)
                self.classes.pop(entity_id, None)

        if len(changes) > 0:
            print(
                f"Time happened changes: {len(changes)}. Discarded {dropped_precision} coarser than day precision "
                f"and {dropped_cutoff} before the cutoff date of {CUTOFFDATE.date()}. "
                f"Entities with time_happened: {len(self.happened)}."
            )

        return changed_entities

    def time_happened(self, entity_id: int) -> int | None:
        values = self.happened.get(entity_id)
        if not values:
            return None
        best_rank = min(rank for _, rank, _ in values)
        return max(t for _, rank, t in values if rank == best_rank)

    def _remove_contribution(self, entity_id: int):
        contribution = self.contributions.pop(entity_id, None)
        if contribution is None:
            return
        age, score, class_ids = contribution
        self.global_totals.remove(age, score)
        for class_id in class_ids:
            totals = self.class_totals[class_id]
            totals.remove(age, score)
            if totals.count == 0:
                del self.class_totals[class_id]

    def calculate_diff(self) -> set[int]:
        """
        Updates the state and returns the entities whose contribution has to be
        recomputed.
        """
        dirty = self.update_happened()
        if not self.initialized:
            # Covers a fresh run as well as a run resumed from cache
            self.initialized = True
            dirty.update(self.happened.keys())
        else:
            dirty.update(self.time_entity_complete.get_changed_entities())
        return dirty

    def calculate(self):
        dirty = self.calculate_diff()
        complete = self.time_entity_complete.get_last_value()
        negative = 0

        for entity_id in dirty:
            self._remove_contribution(entity_id)

            time_complete = complete.get(entity_id)
            time_happened = self.time_happened(entity_id)
            if time_complete is None or time_happened is None:
                continue

            age = int(pd.Timestamp(time_complete).timestamp()) - time_happened
            if age < 0:
                negative += 1
                continue

            score = float(exponential_decay(age, self.exp_factor))
            class_ids = self.classes.get(entity_id, ())
            self.contributions[entity_id] = (age, score, class_ids)
            self.global_totals.add(age, score)
            for class_id in class_ids:
                self.class_totals.setdefault(class_id, Totals()).add(age, score)

        print(
            f"Recomputed {len(dirty)} entities, {negative} of them discarded for a negative completion age. "
            f"Entities in the aggregates: {self.global_totals.count}."
        )

        self.write_result()

    def write_result(self):
        if self.global_totals.count == 0:
            return

        for suffix, value in (
            ("avg_age_years", self.global_totals.avg_age_years()),
            ("avg_score", self.global_totals.avg_score()),
            ("entity_count", self.global_totals.count),
        ):
            value = float(value)
            if math.isfinite(value):
                self.datahandler.write_global_metric_value(f"{self.metric_name}_{suffix}", value)

        writers = {
            suffix: DataHandler.SeriesWriter(self.datahandler, f"{self.metric_name}_{suffix}_per_class", MetricOnClassValueFloat)
            for suffix in ("avg_age_years", "avg_score", "entity_count")
        }
        for class_id, totals in self.class_totals.items():
            for suffix, value in (
                ("avg_age_years", totals.avg_age_years()),
                ("avg_score", totals.avg_score()),
                ("entity_count", totals.count),
            ):
                value = float(value)
                if math.isfinite(value):
                    writers[suffix].add(class_id, value)
        for writer in writers.values():
            writer.write_all()

    def _load_cache(self):
        happened_rows = self.datahandler.get_cache_dict(f"{self.metric_name}_happened", key_length=2)
        for (entity_id, value_id), (property_rank, time_happened) in happened_rows.items():
            self.happened[entity_id] = self.happened.get(entity_id, ()) + ((value_id, property_rank, time_happened),)

        class_rows = self.datahandler.query_postgres(f"SELECT entity_id, class_id FROM cache_{self.metric_name}_classes")
        classes: dict[int, list[int]] = {}
        for entity_id, class_id in class_rows:
            classes.setdefault(entity_id, []).append(class_id)
        self.classes = {entity_id: tuple(sorted(class_ids)) for entity_id, class_ids in classes.items()}

    def save_cache(self):
        self.datahandler.save_cache_rows(
            f"{self.metric_name}_happened",
            [
                (entity_id, value_id, property_rank, time_happened)
                for entity_id, values in self.happened.items()
                for value_id, property_rank, time_happened in values
            ],
            "entity_id BIGINT, value_id TEXT, property_rank INT, time_happened BIGINT, primary key (entity_id, value_id)",
        )
        self.datahandler.save_cache_rows(
            f"{self.metric_name}_classes",
            [(entity_id, class_id) for entity_id, class_ids in self.classes.items() for class_id in class_ids],
            "entity_id BIGINT, class_id INT, primary key (entity_id, class_id)",
        )
