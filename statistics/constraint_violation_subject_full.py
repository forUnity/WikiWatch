from __future__ import annotations

import logging
from collections import defaultdict

import pandas as pd

from datahandler import DataHandler, MetricOnPropertyValueFloat
from statistics.constraint_violation_count import PropertyConstraintViolationCount
from utils.memory_tracker import log_memory_snapshot
from utils.property_constraints import ConstraintRule, _qid_int

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

        def _exceptions(rule: ConstraintRule) -> frozenset[int]:
            return frozenset(
                qid
                for p in rule.params
                if p.qualifierId == P_EXCEPTION
                for qid in [_qid_int(p.qualifierValue)]
                if qid is not None
            )

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
        self.rule_exceptions: dict[ConstraintRule, frozenset[int]] = {
            rule: _exceptions(rule)
            for rules in self.rules_by_property.values()
            for rule in rules
        }

        # entity_id -> direct P31 types
        self.entity_to_types: dict[int, set[int]] = {}
        # type_id -> entities with direct P31=type_id
        self.type_to_entities: dict[int, set[int]] = {}

        # child -> direct parents (P279)
        self.parents_of: dict[int, set[int]] = {}
        # parent -> direct children (reverse P279)
        self.children_of: dict[int, set[int]] = {}
        self._descendants_cache: dict[int, frozenset[int]] = {}

        # entity_id -> {property_id: statement_count}
        self.entity_props: dict[int, dict[int, int]] = {}

        self._watched_props = list(self.property_ids)

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
        # series_writer = DataHandler.SeriesWriter(
        #     self.datahandler,
        #     self.metric_name,
        #     MetricOnPropertyValueFloat,
        # )
        # row_count = 0
        # for row in df.itertuples(index=False):
        #     series_writer.add(int(row.property_id), float(row.violation_count))
        #     row_count += 1
        # series_writer.write_all()
        pass

    def log_memory(self):
        super().log_memory()
        tracked_attrs = {
            "entity_to_types": self.entity_to_types,
            "type_to_entities": self.type_to_entities,
            "parents_of": self.parents_of,
            "children_of": self.children_of,
            "_descendants_cache": self._descendants_cache,
            "entity_props": self.entity_props,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    # ---- cache persistence ----

    def save_cache(self):
        super().save_cache()

        entity_to_types_rows = [(e, t) for e, ts in self.entity_to_types.items() for t in ts]
        self.datahandler.save_cache_rows(
            f"{self.metric_name}_entity_to_types",
            entity_to_types_rows,
            "entity_id BIGINT, type_id BIGINT",
        )

        parents_rows = [(c, p) for c, ps in self.parents_of.items() for p in ps]
        self.datahandler.save_cache_rows(
            f"{self.metric_name}_parents_of",
            parents_rows,
            "child_id BIGINT, parent_id BIGINT",
        )

        entity_props_dict = {
            (e, p): c for e, props in self.entity_props.items() for p, c in props.items()
        }
        self.datahandler.save_cache_dict(
            f"{self.metric_name}_entity_props",
            entity_props_dict,
            "entity_id BIGINT, property_id BIGINT, statement_count INT",
        )

    def _load_stateful_cache(self):
        entity_to_types_cache = self.datahandler.get_cache_dict(f"{self.metric_name}_entity_to_types")
        for eid, tid in entity_to_types_cache.items():
            self.entity_to_types.setdefault(eid, set()).add(tid)
            self.type_to_entities.setdefault(tid, set()).add(eid)

        parents_of_cache = self.datahandler.get_cache_dict(f"{self.metric_name}_parents_of")
        for child, parent in parents_of_cache.items():
            self.parents_of.setdefault(child, set()).add(parent)
            self.children_of.setdefault(parent, set()).add(child)

        entity_props_cache = self.datahandler.get_cache_dict(f"{self.metric_name}_entity_props", 2)
        for (eid, pid), count in entity_props_cache.items():
            self.entity_props.setdefault(eid, {})[pid] = count

    # ---- type graph helpers ----

    def _get_descendants(self, class_id: int) -> frozenset[int]:
        cached = self._descendants_cache.get(class_id)
        if cached is not None:
            return cached

        descendants: set[int] = set()
        stack = list(self.children_of.get(class_id, ()))
        while stack:
            cur = stack.pop()
            if cur in descendants:
                continue
            descendants.add(cur)
            stack.extend(self.children_of.get(cur, ()))

        cached = frozenset(descendants)
        self._descendants_cache[class_id] = cached
        return cached

    def _get_ancestors(
        self,
        child: int,
        local_ancestor_cache: dict[int, frozenset[int]],
    ) -> frozenset[int]:
        cached = local_ancestor_cache.get(child)
        if cached is not None:
            return cached

        ancestors: set[int] = set()
        stack = list(self.parents_of.get(child, ()))
        while stack:
            cur = stack.pop()
            if cur in ancestors:
                continue
            ancestors.add(cur)
            stack.extend(self.parents_of.get(cur, ()))

        cached = frozenset(ancestors)
        local_ancestor_cache[child] = cached
        return cached
    
    def _entity_has_required_type(
    self,
    e: int,
    required_types: tuple[int, ...],
    local_ancestor_cache: dict[int, frozenset[int]],
    ) -> bool:
        if not required_types:
            return True

        types = self.entity_to_types.get(e)
        if not types:
            return False

        for req in required_types:
            if req in types:
                return True

        for t in types:
            ancestors = self._get_ancestors(t, local_ancestor_cache)
            if any(req in ancestors for req in required_types):
                return True

        return False

    def _entity_matches_rule(
        self,
        e: int,
        rule: ConstraintRule,
        local_ancestor_cache: dict[int, frozenset[int]],
    ) -> bool:
        if e in self.rule_exceptions[rule]:
            return True

        required_types = self.rule_required_types[rule]
        if not required_types:
            return True

        relation = self.rule_relations[rule]

        instance_match = self._entity_has_required_type(
            e,
            required_types,
            local_ancestor_cache,
        )

        if relation == Q_RELATION_INSTANCE_OF:
            return instance_match

        ancestors = self._get_ancestors(e, local_ancestor_cache)
        subclass_match = e in required_types or any(t in ancestors for t in required_types)

        if relation == Q_RELATION_SUBCLASS_OF:
            return subclass_match

        if relation == Q_RELATION_INSTANCE_OR_SUBCLASS_OF:
            return instance_match or subclass_match

        return instance_match
    
    def _entity_satisfies_prop(
        self,
        e: int,
        prop: int,
        local_satisfies_cache: dict[int, dict[int, bool]],
        local_ancestor_cache: dict[int, frozenset[int]],
    ) -> bool:
        entity_cache = local_satisfies_cache.get(e)
        if entity_cache is not None:
            cached = entity_cache.get(prop)
            if cached is not None:
                return cached
        else:
            entity_cache = {}
            local_satisfies_cache[e] = entity_cache

        for rule in self.rules_by_property.get(prop, ()):
            if not self._entity_matches_rule(e, rule, local_ancestor_cache):
                entity_cache[prop] = False
                return False

        entity_cache[prop] = True
        return True

    def _entity_violation_count_for_prop(
        self,
        e: int,
        prop: int,
        local_satisfies_cache: dict[int, dict[int, bool]],
        local_ancestor_cache: dict[int, frozenset[int]],
    ) -> int:
        stmt_count = self.entity_props.get(e, {}).get(prop, 0)
        if stmt_count == 0:
            return 0
        return 0 if self._entity_satisfies_prop(
            e,
            prop,
            local_satisfies_cache,
            local_ancestor_cache,
        ) else stmt_count

    def _affected_entities_from_changed_classes(self, class_ids: set[int]) -> set[int]:
        affected_classes: set[int] = set()
        stack = list(class_ids)

        while stack:
            cur = stack.pop()
            if cur in affected_classes:
                continue

            affected_classes.add(cur)

            cached_descendants = self._descendants_cache.get(cur)
            if cached_descendants is not None:
                affected_classes.update(cached_descendants)
                continue

            stack.extend(self.children_of.get(cur, ()))

        for class_id in class_ids:
            self._get_descendants(class_id)

        affected: set[int] = set(affected_classes)
        for c in affected_classes:
            affected.update(self.type_to_entities.get(c, ()))

        return affected

    # ---- batch helpers ----

    def _snapshot_violations(
        self,
        affected_props_by_entity: dict[int, set[int]],
        local_satisfies_cache: dict[int, dict[int, bool]],
        local_ancestor_cache: dict[int, frozenset[int]],
    ) -> dict[int, dict[int, int]]:
        snapshots: dict[int, dict[int, int]] = {}
        for e, props in affected_props_by_entity.items():
            if not props:
                continue
            snapshots[e] = {
                p: self._entity_violation_count_for_prop(
                    e,
                    p,
                    local_satisfies_cache,
                    local_ancestor_cache,
                )
                for p in props
            }
        return snapshots

    def _apply_property_net_changes(
        self,
        prop_net_changes: dict[tuple[int, int], int],
    ) -> None:
        for (e, prop), delta in prop_net_changes.items():
            if delta == 0:
                continue

            current_count = self.entity_props.get(e, {}).get(prop, 0)
            new_count = current_count + delta

            if new_count < 0:
                logger.warning(
                    "[%s] ERROR negative statement count for entity Q%d property P%d; clamping to 0",
                    self.metric_name,
                    e,
                    prop,
                )
                new_count = 0

            if new_count > 0:
                self.entity_props.setdefault(e, {})[prop] = new_count
            else:
                e_props = self.entity_props.get(e)
                if e_props and prop in e_props:
                    del e_props[prop]
                    if not e_props:
                        del self.entity_props[e]

    def _apply_p31_batch(self, p31_net_changes: dict[tuple[int, int], int]) -> None:
        for (e, t), net in p31_net_changes.items():
            if net == 0:
                continue

            base_present = int(t in self.entity_to_types.get(e, set()))
            final_present = 1 if base_present + net > 0 else 0

            if final_present == base_present:
                continue

            if final_present:
                self.entity_to_types.setdefault(e, set()).add(t)
                self.type_to_entities.setdefault(t, set()).add(e)
            else:
                e_types = self.entity_to_types.get(e)
                if e_types is not None:
                    e_types.discard(t)
                    if not e_types:
                        del self.entity_to_types[e]

                rev = self.type_to_entities.get(t)
                if rev is not None:
                    rev.discard(e)
                    if not rev:
                        del self.type_to_entities[t]

    def _apply_p279_batch(self, p279_net_changes: dict[tuple[int, int], int]) -> None:
        for (child, parent), net in p279_net_changes.items():
            if net == 0:
                continue

            base_present = int(parent in self.parents_of.get(child, set()))
            final_present = 1 if base_present + net > 0 else 0

            if final_present == base_present:
                continue

            if final_present:
                self.parents_of.setdefault(child, set()).add(parent)
                self.children_of.setdefault(parent, set()).add(child)
            else:
                parents = self.parents_of.get(child)
                if parents is not None:
                    parents.discard(parent)
                    if not parents:
                        del self.parents_of[child]

                children = self.children_of.get(parent)
                if children is not None:
                    children.discard(child)
                    if not children:
                        del self.children_of[parent]

    # ---- core logic ----

    def calculate_diff(self) -> pd.DataFrame:
        if not self.property_ids:
            return pd.DataFrame(columns=["property_id", "violation_diff"])

        df = self.datahandler.query_duckdb_df(self.deltas_sql, [self._watched_props])
        if df.empty:
            logger.info("[%s] no rows returned", self.metric_name)
            return pd.DataFrame(columns=["property_id", "violation_diff"])

        local_satisfies_cache: dict[int, dict[int, bool]] = {}
        local_ancestor_cache: dict[int, frozenset[int]] = {}

        prop_net_changes: dict[tuple[int, int], int] = defaultdict(int)
        p31_net_changes: dict[tuple[int, int], int] = defaultdict(int)
        p279_net_changes: dict[tuple[int, int], int] = defaultdict(int)

        rules_by_property = self.rules_by_property

        # 1) Collect all batch changes
        for row in df.itertuples(index=False):
            prop = int(row.property_id)
            entity_id = int(row.entity_id)
            action = row.action

            if prop == P_INSTANCE_OF:
                old_t = _qid_int(row.old_value)
                new_t = _qid_int(row.new_value)

                if action != "CREATE" and old_t is not None:
                    p31_net_changes[(entity_id, old_t)] -= 1
                if action != "DELETE" and new_t is not None:
                    p31_net_changes[(entity_id, new_t)] += 1
                continue

            if prop == P_SUBCLASS_OF:
                old_t = _qid_int(row.old_value)
                new_t = _qid_int(row.new_value)

                if action != "CREATE" and old_t is not None:
                    p279_net_changes[(entity_id, old_t)] -= 1
                if action != "DELETE" and new_t is not None:
                    p279_net_changes[(entity_id, new_t)] += 1
                continue

            if prop not in rules_by_property:
                continue

            if action == "CREATE":
                prop_net_changes[(entity_id, prop)] += 1
            elif action == "DELETE":
                prop_net_changes[(entity_id, prop)] -= 1

        # 2) Determine potentially affected entities/properties in the OLD state
        p31_entities = {e for (e, _), net in p31_net_changes.items() if net != 0}
        changed_classes = {child for (child, _), net in p279_net_changes.items() if net != 0}

        affected_props_by_entity: dict[int, set[int]] = defaultdict(set)
        for (e, prop), delta in prop_net_changes.items():
            if delta != 0:
                affected_props_by_entity[e].add(prop)

        affected_entities_before = set(affected_props_by_entity) | p31_entities
        if changed_classes:
            affected_entities_before |= self._affected_entities_from_changed_classes(changed_classes)

        for e in affected_entities_before:
            affected_props_by_entity[e].update(self.entity_props.get(e, ()))

        # 3) Snapshot violations BEFORE applying this timestep
        old_violations = self._snapshot_violations(
            affected_props_by_entity,
            local_satisfies_cache,
            local_ancestor_cache,
        )

        # 4) Apply all changes to reach the NEW state
        self._apply_property_net_changes(prop_net_changes)
        self._apply_p31_batch(p31_net_changes)
        self._apply_p279_batch(p279_net_changes)

        if changed_classes:
            self._descendants_cache.clear()
            local_ancestor_cache.clear()

        local_satisfies_cache.clear()

        # 5) Determine potentially affected entities/properties in the NEW state
        affected_entities_after = set(affected_props_by_entity) | p31_entities
        if changed_classes:
            affected_entities_after |= self._affected_entities_from_changed_classes(changed_classes)

        affected_entities = affected_entities_before | affected_entities_after

        for e in affected_entities:
            affected_props_by_entity[e].update(self.entity_props.get(e, ()))

        # 6) Recompute violations AFTER and subtract
        diff_by_prop: dict[int, int] = defaultdict(int)

        for e in affected_entities:
            props = affected_props_by_entity.get(e)
            if not props:
                continue

            old_for_entity = old_violations.get(e, {})
            for prop in props:
                new_viol = self._entity_violation_count_for_prop(
                    e,
                    prop,
                    local_satisfies_cache,
                    local_ancestor_cache,
                )
                delta = new_viol - old_for_entity.get(prop, 0)
                if delta:
                    diff_by_prop[prop] += delta

        result = (
            pd.DataFrame(
                {
                    "property_id": list(diff_by_prop),
                    "violation_diff": list(diff_by_prop.values()),
                }
            )
            if diff_by_prop
            else pd.DataFrame(columns=["property_id", "violation_diff"])
        )

        logger.info(
            "[%s] processed rows=%d, prop_pairs=%d, p31_entities=%d, p279_classes=%d, result_rows=%d",
            self.metric_name,
            len(df),
            len(prop_net_changes),
            len(p31_entities),
            len(changed_classes),
            len(result),
        )
        return result