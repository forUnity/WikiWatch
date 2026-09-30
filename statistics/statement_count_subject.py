from __future__ import annotations

from metric import Metric
from datahandler import DataHandler
import pandas as pd

from utils.property_constraints import (
    ConstraintRule,
    _qid_int,
    compile_constraint_rules,
    constraint_type_to_property_ids,
    get_wanted_property_constraints,
)
from utils.memory_tracker import log_memory_snapshot

P_EXCEPTION = "P2303"

"""
Counts statements that could violate the subject type constraint.

Important:
- Excludes statements on entities that are exempt from ALL subject-type rules
  of the property via P2303.
- This assumes the same scope/filtering as your numerator.
  If your dataset distinguishes qualifier/reference uses and you only want
  main values, tighten the SQL target filter accordingly.
"""
query = """
SELECT entity_id, property_id, action
FROM value_change
WHERE property_id > 0
  AND property_id = ANY(?)
  AND target IN ('PROPERTY', 'PROPERTY_VALUE', 'ENTITY')
"""


class StatementCountPropertySubject(Metric):
    def __init__(
        self,
        datahandler: DataHandler,
        use_cache: bool,
        metric_name: str,
        extra_property_ids: list[int] | None = None,
    ):
        self.metric_name = metric_name
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []

        if use_cache:
            self.cache: int = self.datahandler.get_cache_int(self.metric_name)
        else:
            self.cache = 0

        wanted_constraint_types = {"Q21503250"}

        json_df = constraint_type_to_property_ids(wanted_constraint_types)
        self.property_ids = [int(pid) for ids in json_df["property_ids"] for pid in ids]

        if extra_property_ids:
            self.property_ids = list(set(self.property_ids) | set(extra_property_ids))

        constraints_df = get_wanted_property_constraints(wanted_constraint_types)
        constraints_df = constraints_df[constraints_df["property_id"].isin(self.property_ids)]
        self.rules_by_property = compile_constraint_rules(constraints_df)

        self.fully_exempt_entities_by_property = self._build_fully_exempt_entities_by_property()

    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
            "fully_exempt_entities_by_property": self.fully_exempt_entities_by_property,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def _rule_exceptions(self, rule: ConstraintRule) -> set[int]:
        return {
            qid
            for p in rule.params
            if p.qualifierId == P_EXCEPTION
            for qid in [_qid_int(p.qualifierValue)]
            if qid is not None
        }

    def _build_fully_exempt_entities_by_property(self) -> dict[int, set[int]]:
        """
        An entity is fully exempt for a property only if it is listed as an
        exception for every subject-type rule on that property.
        """
        fully_exempt: dict[int, set[int]] = {}

        for prop, rules in self.rules_by_property.items():
            if not rules:
                fully_exempt[prop] = set()
                continue

            exempt_intersection: set[int] | None = None

            for rule in rules:
                rule_exceptions = self._rule_exceptions(rule)

                if exempt_intersection is None:
                    exempt_intersection = set(rule_exceptions)
                else:
                    exempt_intersection &= rule_exceptions

                if not exempt_intersection:
                    break

            fully_exempt[prop] = exempt_intersection or set()

        return fully_exempt

    def calculate_diff(self) -> int:
        if not self.property_ids:
            return 0

        df = self.datahandler.query_duckdb_df(query, [self.property_ids])
        if df.empty:
            return 0

        diff_count = 0

        for row in df.itertuples(index=False):
            entity_id = int(row.entity_id)
            property_id = int(row.property_id)

            if entity_id in self.fully_exempt_entities_by_property.get(property_id, ()):
                continue

            if row.action == "CREATE":
                diff_count += 1
            elif row.action == "DELETE":
                diff_count -= 1

        return diff_count

    def calculate(self):
        diff_value = self.calculate_diff()
        self.cache += diff_value
        if self.cache < 0:
            self.cache = 0
        self.write_result()

    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))

    def save_cache(self):
        self.datahandler.save_cache_int(self.metric_name, self.cache)