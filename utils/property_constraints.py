from __future__ import annotations
from dataclasses import dataclass
import json
from typing import Any, Callable, Dict, List, Optional, Tuple, Literal
import pandas as pd

@dataclass(frozen=True)
class ConstraintParam:
    qualifierId: str
    qualifierValue: str


@dataclass(frozen=True)
class ConstraintRule:
    property_id: int
    constraint_type: str
    rank: Optional[str]
    params: Tuple[ConstraintParam, ...]

# ---- load property constraints ----
def prop_qid_to_int( pid: str) -> Optional[int]:
    """
    Convert 'P39' -> 39
    """
    return int(pid[1:])

def _qid_int(v) -> int | None:
    if v is None:
        return None

    s = str(v).strip().strip('"')
    if len(s) < 2 or s[0].upper() != "Q":
        return None

    num = s[1:]
    if not num.isdigit():
        return None

    return int(num)


def get_wanted_property_constraints(wanted_constraint_types) -> pd.DataFrame:
    # TODO: could optimize via caching
    constraints = pd.read_csv("data/property_constraints/property_constraints.csv", dtype=str, sep=";")

    # Convert property_id like "P39" -> 39 (int)
    constraints["property_id"] = (
        constraints["property_id"]
        .apply(prop_qid_to_int)
    )

    # filter to wanted constraint types
    constraints = constraints[constraints["constraint_type"].isin(wanted_constraint_types)]
                            
    return constraints


def constraint_type_to_property_ids(
    wanted_constraint_types: Iterable[str],
) -> pd.DataFrame:
    with open("data/property_constraints/constraint_type_to_properties.json", "r", encoding="utf-8") as f:
        mapping: dict[str, list[str]] = json.load(f)

    df = pd.DataFrame(
        [{"constraint_type": k, "property_ids": v} for k, v in mapping.items()]
    )

    return df.loc[df["constraint_type"].isin(wanted_constraint_types)].copy()




# TODO: use precomputed: data/te.csv
def compile_constraint_rules(
    constraints_df: pd.DataFrame,
) -> Dict[int, List[ConstraintRule]]:
    """
    Build property_id -> [ConstraintRule(...)] from constraint rows.
    Expects constraints_df to already be filtered to the constraint types you care about.
    """
    rules_by_prop: Dict[int, List[ConstraintRule]] = {}

    grouped = constraints_df.groupby(["property_id", "constraint_type", "rank"], dropna=False)

    for (prop, ctype, rank), g in grouped:
        # Build params for this rule
        params = tuple(
            ConstraintParam(str(qid), str(qval))
            for qid, qval in zip(
                g["qualifier_id"].fillna(""),
                g["qualifier_value"].fillna(""),
            )
            if str(qid).strip() != ""
        )

        rules_by_prop.setdefault(prop, []).append(
            ConstraintRule(
                property_id=prop,
                constraint_type=ctype,
                rank=None if pd.isna(rank) else rank,
                params=params,
            )
        )

    return rules_by_prop



# to convert the weird strings to floats
def coerce_numeric(v: Any) -> Optional[float]:
    """
    Return float if v looks like a number, else None.
    """
    if v is None:
        return None

    # ignore structured values ({} or geo dicts)
    if isinstance(v, dict):
        return None

    # accept ints/floats directly
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return float(v)

    # strings (including ones that *look* like '"7"')
    s = str(v).strip()
    if not s or s == "{}":
        return None

    # remove one layer of wrapping quotes
    if len(s) >= 2 and s[0] in ("'", '"') and s[-1] == s[0]:
        s = s[1:-1].strip()

    # parse numeric
    try:
        return float(s)   # handles "+0.98", "7"
    except ValueError:
        return None




def first_float_param(rule: ConstraintRule, qualifier_id: str) -> Optional[float]:
    """
    Extract first float-ish qualifierValue for a given qualifierId.
    Returns None if missing/unparseable.
    """
    for p in rule.params:
        if p.qualifierId == qualifier_id:
            s = (p.qualifierValue or "").strip()
            if not s:
                continue
            try:
                return float(s)
            except Exception:
                return None
    return None

Side = Literal["old", "new"]

def count_constraint_violation_diff_from_changes(
    value_changes: pd.DataFrame,
    rules_by_property: Dict[int, List["ConstraintRule"]],
    violates_fn: Callable[["ConstraintRule", pd.Series, Side], bool],
) -> pd.DataFrame:
    """
    Per-property net diff in *any* constraint violations, evaluating all rules for that property.

    For each value_change row:
      violates_new = True if ANY rule for that property is violated by the NEW value (or row context)
      violates_old = True if ANY rule for that property is violated by the OLD value (or row context)

      delta = 1[violates_new] - 1[violates_old]

    Notes:
    - This counts at most 1 violation per row per side (even if multiple rules are violated).
    - violates_fn should return False for rules it cannot evaluate for this row.
    """
    diff_by_prop: Dict[int, int] = {}

    # iterate rows as Series so callback can inspect multiple fields
    for _, row in value_changes.iterrows():
        prop_id = row.get("property_id") # need str keys for dict lookup
        rules = rules_by_property.get(prop_id)
        if not rules:
            continue

        # violates_old: “Was the old value violating the constraint?”
        # violates_new: “Is the new value violating the constraint?”
        # => cause we want to track net changes in violations
        violates_new = False
        violates_old = False

        # Evaluate ALL rules (short-circuit once True)
        for rule in rules:
            if not violates_new and violates_fn(rule, row, "new"):
                violates_new = True
            if not violates_old and violates_fn(rule, row, "old"):
                violates_old = True

            if violates_new and violates_old:
                break

        if not violates_new and not violates_old:
            continue

        # here we track the net difference
        delta =int(violates_new) - int(violates_old) # -1, 0, +1
        if delta:
            diff_by_prop[prop_id] = diff_by_prop.get(prop_id, 0) + delta
    if not diff_by_prop:
        return pd.DataFrame(columns=["property_id", "violation_diff"])
    
    props = list(diff_by_prop.keys())
    return pd.DataFrame({
        "property_id": props,
        "violation_diff": [diff_by_prop.get(p, 0) for p in props],
    })
