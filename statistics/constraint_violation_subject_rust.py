from __future__ import annotations

import logging

import pandas as pd

from datahandler import DataHandler, MetricOnPropertyValueFloat
from statistics.constraint_violation_count import PropertyConstraintViolationCount
from utils.memory_tracker import log_memory_snapshot
from utils.property_constraints import ConstraintRule, _qid_int

from subject_constraint_core import SubjectConstraintCore

P_CLASS = "P2308"
P_RELATION = "P2309"
P_EXCEPTION = "P2303"

P_INSTANCE_OF = 31
P_SUBCLASS_OF = 279

Q_RELATION_INSTANCE_OF = 21503252
Q_RELATION_SUBCLASS_OF = 21514624
Q_RELATION_INSTANCE_OR_SUBCLASS_OF = 30208840

_DELTAS_SQL = """
SELECT entity_id, property_id, action, old_value, new_value
FROM value_change
WHERE property_id > 0
  AND (property_id = ANY(?) OR property_id IN (31, 279))
  AND target IN ('PROPERTY', 'PROPERTY_VALUE', 'ENTITY')
"""

logger = logging.getLogger(__name__)


class SubjectConstraintViolationCount(PropertyConstraintViolationCount):
    metric_name = "subject_constraint_violation_count"
    wanted_constraint_types = {"Q21503250"}
    deltas_sql = _DELTAS_SQL

    def __init__(self, datahandler, use_cache: bool):
        super().__init__(datahandler, use_cache)

        def _required_types(rule: ConstraintRule) -> list[int]:
            return [
                int(p.qualifierValue[1:])
                for p in rule.params
                if p.qualifierId == P_CLASS
                and p.qualifierValue
                and p.qualifierValue.startswith("Q")
                and p.qualifierValue[1:].isdigit()
            ]

        def _relation(rule: ConstraintRule) -> int:
            for p in rule.params:
                if (
                    p.qualifierId == P_RELATION
                    and p.qualifierValue
                    and p.qualifierValue.startswith("Q")
                    and p.qualifierValue[1:].isdigit()
                ):
                    return int(p.qualifierValue[1:])
            return Q_RELATION_INSTANCE_OR_SUBCLASS_OF

        def _exceptions(rule: ConstraintRule) -> list[int]:
            return [
                int(qid)
                for p in rule.params
                if p.qualifierId == P_EXCEPTION
                for qid in [_qid_int(p.qualifierValue)]
                if qid is not None
            ]

        self.rule_required_types: dict[ConstraintRule, tuple[int, ...]] = {
            rule: tuple(_required_types(rule))
            for rules in self.rules_by_property.values()
            for rule in rules
        }
        self.rule_relations: dict[ConstraintRule, int] = {
            rule: _relation(rule)
            for rules in self.rules_by_property.values()
            for rule in rules
        }
        self.rule_exceptions: dict[ConstraintRule, tuple[int, ...]] = {
            rule: tuple(_exceptions(rule))
            for rules in self.rules_by_property.values()
            for rule in rules
        }

        # Python mirrors, mainly for logging/debug compatibility.
        self.entity_to_types: dict[int, set[int]] = {}
        self.type_to_entities: dict[int, set[int]] = {}
        self.parents_of: dict[int, set[int]] = {}
        self.children_of: dict[int, set[int]] = {}
        self.entity_props: dict[int, dict[int, int]] = {}

        self._watched_props = list(self.property_ids)

        # property -> list of rules
        # rule = (required_types, relation, exceptions)
        rules_by_property_for_core: list[tuple[int, list[tuple[list[int], int, list[int]]]]] = []
        for prop, rules in self.rules_by_property.items():
            rules_by_property_for_core.append(
                (
                    int(prop),
                    [
                        (
                            list(self.rule_required_types[rule]),
                            int(self.rule_relations[rule]),
                            list(self.rule_exceptions[rule]),
                        )
                        for rule in rules
                    ],
                )
            )

        self.core = SubjectConstraintCore(
            property_ids=[int(p) for p in self.property_ids],
            rules_by_property=rules_by_property_for_core,
        )

        if use_cache:
            self._load_stateful_cache()

        logger.info(
            "[%s] initialized (rules=%d, watched_props=%d, use_cache=%s)",
            self.metric_name,
            len(self.rule_required_types),
            len(self._watched_props),
            use_cache,
        )

    def write_result(self, df: pd.DataFrame):
        # can comment out if not needed
        series_writer = DataHandler.SeriesWriter(
            self.datahandler,
            self.metric_name,
            MetricOnPropertyValueFloat,
        )
        row_count = 0
        for row in df.itertuples(index=False):
            series_writer.add(int(row.property_id), float(row.violation_count))
            row_count += 1
        series_writer.write_all()

    def log_memory(self):
        self._refresh_python_state_from_core()

        super().log_memory()
        tracked_attrs = {
            "entity_to_types": self.entity_to_types,
            "type_to_entities": self.type_to_entities,
            "parents_of": self.parents_of,
            "children_of": self.children_of,
            "entity_props": self.entity_props,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    # ---- cache persistence ----

    def save_cache(self):
        super().save_cache()

        entity_to_types_rows = self.core.export_entity_to_types()
        self.datahandler.save_cache_rows(
            f"{self.metric_name}_entity_to_types",
            entity_to_types_rows,
            "entity_id BIGINT, type_id BIGINT",
        )

        parents_rows = self.core.export_parents_of()
        self.datahandler.save_cache_rows(
            f"{self.metric_name}_parents_of",
            parents_rows,
            "child_id BIGINT, parent_id BIGINT",
        )

        entity_props_rows = self.core.export_entity_props()
        entity_props_dict = {
            (int(eid), int(pid)): int(count)
            for eid, pid, count in entity_props_rows
        }
        self.datahandler.save_cache_dict(
            f"{self.metric_name}_entity_props",
            entity_props_dict,
            "entity_id BIGINT, property_id BIGINT, statement_count INT",
        )

    def _load_stateful_cache(self):
        entity_to_types_cache = self.datahandler.get_cache_dict(f"{self.metric_name}_entity_to_types")
        entity_to_types_rows = [(int(eid), int(tid)) for eid, tid in entity_to_types_cache.items()]

        parents_of_cache = self.datahandler.get_cache_dict(f"{self.metric_name}_parents_of")
        parents_rows = [(int(child), int(parent)) for child, parent in parents_of_cache.items()]

        entity_props_cache = self.datahandler.get_cache_dict(f"{self.metric_name}_entity_props", 2)
        entity_props_rows = [
            (int(eid), int(pid), int(count))
            for (eid, pid), count in entity_props_cache.items()
        ]

        self.core.load_entity_to_types(entity_to_types_rows)
        self.core.load_parents_of(parents_rows)
        self.core.load_entity_props(entity_props_rows)

        self._refresh_python_state_from_core()

    def _refresh_python_state_from_core(self):
        self.entity_to_types = {}
        self.type_to_entities = {}
        for eid, tid in self.core.export_entity_to_types():
            self.entity_to_types.setdefault(int(eid), set()).add(int(tid))
            self.type_to_entities.setdefault(int(tid), set()).add(int(eid))

        self.parents_of = {}
        self.children_of = {}
        for child, parent in self.core.export_parents_of():
            self.parents_of.setdefault(int(child), set()).add(int(parent))
            self.children_of.setdefault(int(parent), set()).add(int(child))

        self.entity_props = {}
        for eid, pid, count in self.core.export_entity_props():
            self.entity_props.setdefault(int(eid), {})[int(pid)] = int(count)


    # ---- core logic ----

    def calculate_diff(self) -> pd.DataFrame:
        if not self.property_ids:
            return pd.DataFrame(columns=["property_id", "violation_diff"])

        df = self.datahandler.query_duckdb_df(self.deltas_sql, [self._watched_props])
        if df.empty:
            logger.info("[%s] no rows returned", self.metric_name)
            return pd.DataFrame(columns=["property_id", "violation_diff"])

        entity_ids = [int(v) for v in df["entity_id"].tolist()]
        property_ids = [int(v) for v in df["property_id"].tolist()]
        actions = [1 if v == "CREATE" else -1 if v == "DELETE" else 0 for v in df["action"].tolist()]

        # 0 means "no QID" for the Rust boundary
        old_values = df["old_value"].tolist()
        new_values = df["new_value"].tolist()

        old_qids: list[int] = []
        new_qids: list[int] = []

        for prop, old_v, new_v in zip(property_ids, old_values, new_values):
            if prop == P_INSTANCE_OF or prop == P_SUBCLASS_OF:
                old_q = _qid_int(old_v)
                new_q = _qid_int(new_v)
                old_qids.append(int(old_q) if old_q is not None else 0)
                new_qids.append(int(new_q) if new_q is not None else 0)
            else:
                old_qids.append(0)
                new_qids.append(0)

        diff_rows = self.core.process_batch(
            entity_ids=entity_ids,
            property_ids=property_ids,
            actions=actions,
            old_qids=old_qids,
            new_qids=new_qids,
        )

        result = (
            pd.DataFrame(diff_rows, columns=["property_id", "violation_diff"])
            if diff_rows
            else pd.DataFrame(columns=["property_id", "violation_diff"])
        )

        logger.info(
            "[%s] processed rows=%d, result_rows=%d",
            self.metric_name,
            len(df),
            len(result),
        )
        return result