# not ready

"""Symmetric & inverse property constraint violation count.

Tracks per-property violation counts for:
  - Symmetric constraint (Q21510862): if (A, P, B) exists then (B, P, A) must exist.
  - Inverse constraint  (Q21510855): if (A, P, B) exists then (B, P', A) must exist,
    where P' is specified via qualifier P2306.

Maintains an in-memory index of entity statements for all watched properties so
that cross-entity reciprocal lookups are O(1).
"""

from __future__ import annotations

import logging
import pandas as pd

from statistics.constraint_violation_count import (
    CACHE_COLUMNS,
    PropertyConstraintViolationCount,
)
from utils.property_constraints import ConstraintRule, Side, _qid_int, prop_qid_to_int
from utils.memory_tracker import log_memory_snapshot

P_PROPERTY = "P2306"  # qualifier that specifies the inverse property

_DELTAS_SQL = """
SELECT entity_id, property_id, old_value, new_value
FROM value_change
WHERE property_id = ANY(?)
  AND target IN ('PROPERTY', 'PROPERTY_VALUE', 'ENTITY')
"""

logger = logging.getLogger(__name__)


def _inverse_property_id(rule: ConstraintRule) -> int | None:
    """Extract the inverse property id from a rule's P2306 qualifier."""
    for p in rule.params:
        if p.qualifierId == P_PROPERTY and p.qualifierValue:
            v = p.qualifierValue
            return prop_qid_to_int(v) if v.startswith("P") else None
    return None


class SymmetricInverseConstraintViolationCount(PropertyConstraintViolationCount):
    metric_name = "symmetric_inverse_constraint_violation_count"
    wanted_constraint_types = {"Q21510862", "Q21510855"}
    deltas_sql = _DELTAS_SQL

    def __init__(self, datahandler, use_cache: bool):
        super().__init__(datahandler, use_cache)

        # Bidirectional inverse map: property_id -> inverse_property_id.
        # For symmetric (Q21510862): P -> P  (inverse is self).
        # For inverse   (Q21510855): P -> P' and P' -> P.
        self.inverse_pairs: dict[int, int] = {}

        for prop, rules in self.rules_by_property.items():
            for rule in rules:
                if rule.constraint_type == "Q21510862":
                    self.inverse_pairs[prop] = prop
                elif rule.constraint_type == "Q21510855":
                    inv = _inverse_property_id(rule)
                    if inv is not None:
                        self.inverse_pairs[prop] = inv
                        self.inverse_pairs.setdefault(inv, prop)

        # The full set of properties we must watch (includes inverse partners)
        self.all_watched: list[int] = list(
            set(self.property_ids) | set(self.inverse_pairs.values())
        )

        # entity_id -> {property_id -> set of value_entity_ids}
        # Tracks current state of all relevant entity-value statements.
        self.entity_stmts: dict[int, dict[int, set[int]]] = {}

        if use_cache:
            self._load_stateful_cache()
            
    def log_memory(self):
        super().log_memory()
        tracked_attrs = {
            "entity_stmts": self.entity_stmts,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    # ---- helpers ----

    def _get_inverse_prop(self, prop: int) -> int | None:
        """Return the expected reciprocal property for *prop*, or None.

        - Symmetric property  → returns prop itself.
        - Inverse-constrained → returns the partner property.
        - Not constrained     → None.
        """
        return self.inverse_pairs.get(prop)

    def _has_reciprocal(self, entity_a: int, prop: int, entity_b: int) -> bool:
        """Check if the reciprocal statement for (A, P, B) exists."""
        inv_prop = self._get_inverse_prop(prop)
        if inv_prop is None:
            return True  # property not constrained
        b_props = self.entity_stmts.get(entity_b)
        if b_props is None:
            return False
        return entity_a in b_props.get(inv_prop, set())

    def _add_stmt(self, entity: int, prop: int, value: int):
        self.entity_stmts.setdefault(entity, {}).setdefault(prop, set()).add(value)

    def _remove_stmt(self, entity: int, prop: int, value: int):
        props = self.entity_stmts.get(entity)
        if props is None:
            return
        vals = props.get(prop)
        if vals is None:
            return
        vals.discard(value)
        if not vals:
            del props[prop]
        if not props:
            del self.entity_stmts[entity]

    def _is_constrained_by_inverse(self, prop: int) -> bool:
        """Return True if this property has a symmetric or inverse constraint."""
        return prop in self.inverse_pairs

    # ---- cache persistence ----

    def save_cache(self):
        super().save_cache()
        rows = [
            (eid, pid, vid)
            for eid, props in self.entity_stmts.items()
            for pid, vals in props.items()
            for vid in vals
        ]
        df = pd.DataFrame(rows, columns=["entity_id", "property_id", "value_id"])
        self.datahandler.save_cache_df(f"{self.metric_name}_entity_stmts", df)

    def _load_stateful_cache(self):
        df = self.datahandler.get_cache_df(f"{self.metric_name}_entity_stmts")
        for eid, pid, vid in df.itertuples(index=False):
            self.entity_stmts.setdefault(eid, {}).setdefault(pid, set()).add(vid)

    # ---- violation check (unused by our custom calculate_diff, required by ABC) ----

    def violates(self, rule: ConstraintRule, row: pd.Series, side: Side) -> bool:
        # Not used — we override calculate_diff entirely.
        return False

    # ---- core logic ----

    def calculate_diff(self) -> pd.DataFrame:
        if not self.all_watched:
            return pd.DataFrame(columns=["property_id", "violation_diff"])

        df = self.datahandler.query_duckdb_df(self.deltas_sql, [self.all_watched])
        if df.empty:
            return pd.DataFrame(columns=["property_id", "violation_diff"])

        diff_by_prop: dict[int, int] = {}

        for _, row in df.iterrows():
            prop = int(row["property_id"])
            entity_a = int(row["entity_id"])

            # Parse value entity id from new_value / old_value
            entity_b_new = _qid_int(row.get("new_value"))
            entity_b_old = _qid_int(row.get("old_value"))

            if entity_b_new is not None:
                self._handle_create(entity_a, prop, entity_b_new, diff_by_prop)

            if entity_b_old is not None:
                self._handle_delete(entity_a, prop, entity_b_old, diff_by_prop)


        return (
            pd.DataFrame(
                {
                    "property_id": list(diff_by_prop),
                    "violation_diff": list(diff_by_prop.values()),
                }
            )
            if diff_by_prop
            else pd.DataFrame(columns=["property_id", "violation_diff"])
        )

    def _handle_create(
        self,
        entity_a: int,
        prop: int,
        entity_b: int,
        diff: dict[int, int],
    ):
        """Process creation of statement (A, P, B)."""
        inv_prop = self._get_inverse_prop(prop)

        # 1) Add the statement to our index
        self._add_stmt(entity_a, prop, entity_b)

        # 2) Does my new statement lack its reciprocal?:
        # Does my new statement satisfy someone else's reciprocal requirement?        
        if self._is_constrained_by_inverse(prop):
            if not self._has_reciprocal(entity_a, prop, entity_b):
                diff[prop] = diff.get(prop, 0) + 1

            b_vals = self.entity_stmts.get(entity_b, {}).get(inv_prop, set())
            if entity_a in b_vals:
                diff[inv_prop] = diff.get(inv_prop, 0) - 1

    def _handle_delete(
        self,
        entity_a: int,
        prop: int,
        entity_b: int,
        diff: dict[int, int],
    ):
        """Process deletion of statement (A, P, B)."""
        inv_prop = self._get_inverse_prop(prop)

        if self._is_constrained_by_inverse(prop):
            #  If the reciprocal was missing, removing this
            #    statement removes that violation.
            if not self._has_reciprocal(entity_a, prop, entity_b):
                diff[prop] = diff.get(prop, 0) - 1

            self._remove_stmt(entity_a, prop, entity_b)

            # If B has (B, inv_prop, A) and that statement's reciprocal was
            #    fulfilled by (A, prop, B), then removing it creates a new violation.
            b_vals = self.entity_stmts.get(entity_b, {}).get(inv_prop, set())
            if entity_a in b_vals:
                diff[inv_prop] = diff.get(inv_prop, 0) + 1
        else:
            self._remove_stmt(entity_a, prop, entity_b)

    def calculate(self):
        df = self.calculate_diff()
        if df.empty:
            self.write_result(self.cache)
            return

        diff_series = df.set_index("property_id")["violation_diff"]
        self._last_series = self._last_series.add(diff_series, fill_value=0)

        self.cache = self._last_series.reset_index()
        self.cache.columns = list(CACHE_COLUMNS)
        self.write_result(self.cache)
