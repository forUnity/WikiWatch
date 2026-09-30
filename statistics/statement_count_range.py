from metric import Metric
from datahandler import DataHandler
from utils.property_constraints import (
    compile_constraint_rules,
    get_wanted_property_constraints,
)
from utils.memory_tracker import log_memory_snapshot

"""
Warning when not starting from start and not using cache: updates are not accounted for,
so will differ from metrics that do.
"""

query = """
SELECT
  (SELECT COUNT(*)
   FROM value_change
   WHERE
     target IN ('PROPERTY', 'PROPERTY_VALUE', 'ENTITY')
     AND "action" = 'CREATE'
     AND property_id = ANY(?)
  )
  -
  (SELECT COUNT(*)
   FROM value_change
   WHERE
     target IN ('PROPERTY', 'PROPERTY_VALUE', 'ENTITY')
     AND "action" = 'DELETE'
     AND property_id = ANY(?)
  ) AS diff_count
"""

P_CONSTRAINT_STATUS = "P2316"
Q_MANDATORY_CONSTRAINT = "Q21502408"
Q_SUGGESTION_CONSTRAINT = "Q62026391"


def _rule_status(rule) -> str:
    for param in rule.params:
        if param.qualifierId != P_CONSTRAINT_STATUS:
            continue
        if param.qualifierValue == Q_MANDATORY_CONSTRAINT:
            return "mandatory"
        if param.qualifierValue == Q_SUGGESTION_CONSTRAINT:
            return "suggestion"
    return "regular"


class StatementCountProperty(Metric):
    """
    Running total of statements which should conform to a property constraint.
    """

    def __init__(
        self,
        datahandler: DataHandler,
        use_cache: bool,
        WANTED_CONSTRAINT_TYPES,
        metric_name,
        extra_property_ids: list[int] | None = None,
        constraint_status_filter: str | None = None,   # None | "mandatory" | "regular" | "suggestion"
    ):
        self.metric_name = metric_name
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []
        self.constraint_status_filter = constraint_status_filter

        if use_cache:
            try:
                self.cache: int = self.datahandler.get_cache_int(self.metric_name)
            except ValueError:
                self.cache = 0
        else:
            self.cache = 0

        constraints_df = get_wanted_property_constraints(WANTED_CONSTRAINT_TYPES)
        rules_by_property = compile_constraint_rules(constraints_df)

        if self.constraint_status_filter is None:
            filtered_rules_by_property = rules_by_property
        else:
            filtered_rules_by_property = {}

            for property_id, rules in rules_by_property.items():
                filtered_rules = [
                    rule for rule in rules
                    if _rule_status(rule) == self.constraint_status_filter
                ]
                if filtered_rules:
                    filtered_rules_by_property[int(property_id)] = filtered_rules

        property_ids: set[int] = set(int(pid) for pid in filtered_rules_by_property.keys())

        if extra_property_ids:
            property_ids |= set(extra_property_ids)

        self.property_ids = sorted(property_ids)

    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def calculate_diff(self) -> int:
        if not self.property_ids:
            return 0

        df = self.datahandler.query_duckdb_df(query, [self.property_ids, self.property_ids])
        return int(df["diff_count"].iloc[0]) if not df.empty else 0

    def calculate(self):
        diff_value = self.calculate_diff()
        self.cache += diff_value
        self.write_result()

    def write_result(self):
        self.datahandler.write_global_metric_value(self.metric_name, float(self.cache))

    def save_cache(self):
        self.datahandler.save_cache_int(self.metric_name, self.cache)