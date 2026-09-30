from __future__ import annotations


import pandas as pd

from metric import Metric
from statistics.constraint_violation_count import (
    CACHE_COLUMNS,
    PropertyConstraintViolationCount,
)
from utils.property_constraints import (
    ConstraintRule,
    compile_constraint_rules,
    get_wanted_property_constraints,
)

import re
from datetime import datetime, timezone

from statistics.constraint_violation_count import PropertyConstraintViolationCount
from utils.property_constraints import ConstraintRule, coerce_numeric

P_MAX_VALUE = "P2312"
P_MIN_VALUE = "P2313"
P_MIN_DATE = "P2310"
P_MAX_DATE = "P2311"
P_CONSTRAINT_STATUS = "P2316"

Q_MANDATORY_CONSTRAINT = "Q21502408"
Q_SUGGESTION_CONSTRAINT = "Q62026391"

_DELTAS_AGG_SQL_RANGE = """
SELECT property_id, old_value, new_value
FROM value_change
WHERE target IN ('PROPERTY', 'PROPERTY_VALUE', 'ENTITY')
  AND property_id = ANY(?)
"""

_DATE_RE = re.compile(r'^\s*([+-]?)(\d+)-(\d{2})-(\d{2})')


def _first_param_value(rule: ConstraintRule, qualifier_id: str) -> str | None:
    for param in rule.params:
        if param.qualifierId == qualifier_id:
            return param.qualifierValue
    return None


def _has_param(rule: ConstraintRule, qualifier_id: str) -> bool:
    return any(param.qualifierId == qualifier_id for param in rule.params)


def _first_numeric_param(rule: ConstraintRule, qualifier_id: str) -> float | None:
    raw = _first_param_value(rule, qualifier_id)
    if raw is None:
        return None
    return coerce_numeric(raw)


def _today_tuple() -> tuple[int, int, int]:
    today = datetime.now(timezone.utc).date()
    return (today.year, today.month, today.day)


def _parse_wikidata_date(
    raw: object,
    *,
    somevalue_means_today: bool,
) -> tuple[int, int, int] | None:
    if raw is None:
        return None

    s = str(raw).strip().strip('"')
    upper = s.upper()

    # Common sentinels used in extracted constraint tables / dumps
    if upper in {"%NOVALUE", "NOVALUE", "NO VALUE"}:
        return None

    if upper in {"%SOMEVALUE", "SOMEVALUE", "UNKNOWN VALUE", "UNKNOWN"}:
        return _today_tuple() if somevalue_means_today else None

    # Matches:
    #   +1957-10-04T00:00:00Z
    #   -3190000-00-00
    #   2024-01-31
    m = _DATE_RE.match(s)
    if not m:
        return None

    sign = -1 if m.group(1) == "-" else 1
    year = sign * int(m.group(2))
    month = int(m.group(3))
    day = int(m.group(4))
    return (year, month, day)


def _rule_status(rule: ConstraintRule) -> str:
    status_values = {
        str(param.qualifierValue)
        for param in rule.params
        if param.qualifierId == P_CONSTRAINT_STATUS and param.qualifierValue is not None
    }

    if Q_MANDATORY_CONSTRAINT in status_values:
        return "mandatory"
    if Q_SUGGESTION_CONSTRAINT in status_values:
        return "suggestion"
    return "regular"


class _RangeConstraintViolationCountBase(PropertyConstraintViolationCount):
    wanted_constraint_types = {"Q21510860"}
    deltas_sql = _DELTAS_AGG_SQL_RANGE
    status_filter: str | None = None

    def __init__(self, datahandler, use_cache: bool):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []

        if use_cache:
            try:
                self.cache: pd.DataFrame = self.datahandler.get_cache_df(self.metric_name)
            except ValueError:
                self.cache = pd.DataFrame(columns=CACHE_COLUMNS)
        else:
            self.cache = pd.DataFrame(columns=CACHE_COLUMNS)

        # source of truth = property_constraints.csv via helper
        constraints_df = get_wanted_property_constraints(self.wanted_constraint_types)

        all_rules_by_property = compile_constraint_rules(constraints_df)

        if self.status_filter is None:
            self.rules_by_property = all_rules_by_property
        else:
            filtered_rules_by_property: dict[int, list[ConstraintRule]] = {}

            for property_id, rules in all_rules_by_property.items():
                filtered_rules = [
                    rule for rule in rules
                    if _rule_status(rule) == self.status_filter
                ]
                if filtered_rules:
                    filtered_rules_by_property[int(property_id)] = filtered_rules

            self.rules_by_property = filtered_rules_by_property

        self.property_ids = sorted(int(pid) for pid in self.rules_by_property.keys())

        self._last_series = (
            self.cache.set_index("property_id")["violation_count"]
            if not self.cache.empty
            else pd.Series(dtype="float64")
        )

    def violates(self, rule: ConstraintRule, row, side):
        raw = row["new_value"] if side == "new" else row["old_value"]

        violated = False
        applied = False

        # Quantity bounds: P2313 / P2312
        if _has_param(rule, P_MIN_VALUE) or _has_param(rule, P_MAX_VALUE):
            v = coerce_numeric(raw)
            if v is not None:
                applied = True
                lo = _first_numeric_param(rule, P_MIN_VALUE)
                hi = _first_numeric_param(rule, P_MAX_VALUE)
                if (lo is not None and v < lo) or (hi is not None and v > hi):
                    violated = True

        # Date bounds: P2310 / P2311
        if _has_param(rule, P_MIN_DATE) or _has_param(rule, P_MAX_DATE):
            d = _parse_wikidata_date(raw, somevalue_means_today=False)
            if d is not None:
                applied = True
                lo = _parse_wikidata_date(
                    _first_param_value(rule, P_MIN_DATE),
                    somevalue_means_today=True,
                )
                hi = _parse_wikidata_date(
                    _first_param_value(rule, P_MAX_DATE),
                    somevalue_means_today=True,
                )
                if (lo is not None and d < lo) or (hi is not None and d > hi):
                    violated = True

        return violated if applied else False


class RangeConstraintViolationCount(_RangeConstraintViolationCountBase):
    metric_name = "range_constraint_violation_count"
    status_filter = None


class RangeConstraintViolationCountMandatory(_RangeConstraintViolationCountBase):
    metric_name = "range_constraint_violation_count_mandatory"
    status_filter = "mandatory"


class RangeConstraintViolationCountRegular(_RangeConstraintViolationCountBase):
    metric_name = "range_constraint_violation_count_regular"
    status_filter = "regular"


class RangeConstraintViolationCountSuggestion(_RangeConstraintViolationCountBase):
    metric_name = "range_constraint_violation_count_suggestion"
    status_filter = "suggestion"