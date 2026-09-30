"""
Shared parsing and scope rules for Wikidata time values.

Both the data age at insertion metric and the completeness currency metric read
'time'-typed values out of value_change.new_value, and both need the same two
answers: does this value parse, and is it in scope?

They used to answer those questions separately -- one in Python, one in DuckDB
SQL -- and drifted apart: the SQL side cut off at year > 2012 instead of the
2012-10-29 start of Wikidata, coerced coarse precisions to Jan 1 instead of
discarding them, and only stripped a leading '+' when it was directly preceded
by a JSON quote. The rules live here so that cannot happen again.
"""

from __future__ import annotations

from typing import Final

import pandas as pd

# Wikidata was created on the 29th of October 2012; values dated before that
# are outside the scope of every metric built on this data.
CUTOFFDATE: Final[pd.Timestamp] = pd.Timestamp("2012-10-29T00:00:00Z")


def parse_time_value(value) -> pd.Timestamp | None:
    """
    Parse a raw value_change time value into a timestamp, or None if it does
    not represent a usable date.

    The value arrives as it is stored: a JSON string, so usually quoted and
    with the Wikidata leading '+', e.g. '"+2013-05-01T00:00:00Z"'.
    """
    if value is None or pd.isna(value):
        return None

    if isinstance(value, str):
        value = value.strip()

        while (
            len(value) >= 2
            and ((value[0] == value[-1] == "'") or (value[0] == value[-1] == '"'))
        ):
            value = value[1:-1].strip()

        if value in ("{}", "", "null", "None"):
            return None

        if value.startswith("+"):
            value = value[1:]

        # Precision filter: only dates that are precise to at least the
        # day are in scope. Wikidata writes coarser precisions with a
        # zeroed month and/or day component, e.g.
        #   +2026-00-00T00:00:00Z  (year precision)
        #   +2026-07-00T00:00:00Z  (month precision)
        # Both are discarded rather than coerced to a concrete day,
        # which would fabricate a delay.
        if len(value) >= 10 and value[4] == "-" and value[7] == "-":
            if value[5:7] == "00" or value[8:10] == "00":
                return None

    ts = pd.to_datetime(value, utc=True, errors="coerce")

    if pd.isna(ts):
        return None

    return ts


def is_in_scope(value_time: pd.Timestamp | None) -> bool:
    """Whether a parsed time value is at or after the start of Wikidata."""
    return value_time is not None and value_time >= CUTOFFDATE


def parse_time_value_in_scope(value) -> pd.Timestamp | None:
    """
    Parse a raw value_change time value and drop it if it falls before
    CUTOFFDATE. Returns None in both the unparsable and the out-of-scope case;
    use parse_time_value plus is_in_scope where the two need to be told apart.
    """
    value_time = parse_time_value(value)

    return value_time if is_in_scope(value_time) else None
