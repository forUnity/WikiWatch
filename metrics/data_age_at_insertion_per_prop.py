"""
Data Age at Insertion

Property-level outputs:
    data_age_at_insertion_scaled_score
    data_age_at_insertion_avg_delay_days
    data_age_at_insertion_late_statement_count
    data_age_at_insertion_too_old_statement_count
    data_age_at_insertion_statement_count          (denominator of the score)
    data_age_at_insertion_future_statement_count   (subset dated after insertion)

Global statement-weighted output:
    data_age_at_insertion_statement_average_scaled_score

Logic:
    Each active statement contributes at most one current score.

    CREATE:
        contribution is based on CREATE timestamp vs written time value.

    UPDATE:
        old contribution is removed.
        new contribution is based on UPDATE timestamp vs written time value.

    DELETE:
        contribution is removed.

    EXPIRY:
        contribution is removed after MAX_DELAY_DAYS since its latest write.

    PRECISION:
        only time values with a precision of at least "day" are considered.
        Wikidata encodes coarser precisions with a zeroed month and/or day
        component (e.g. +2026-00-00T00:00:00Z); those values are discarded.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import timedelta
from heapq import heappop, heappush
from typing import Final, Optional

import pandas as pd

from metric import Metric
from datahandler import (
    DataHandler,
    Dimension,
    MetricOnPropertyValueFloat,
    MetricValueFloat,
)
from utils.memory_tracker import log_memory_snapshot
from utils.time_values import CUTOFFDATE, parse_time_value

GOAL_DAYS: Final[int] = 3
MAX_DELAY_DAYS: Final[int] = 90

DELTAS_SQL: Final[str] = """
SELECT
    timestamp,
    value_id,
    entity_id,
    property_id,
    old_value,
    new_value,
    old_datatype,
    new_datatype,
    action
FROM value_change
WHERE old_datatype = 'time'
   OR new_datatype = 'time'
ORDER BY timestamp ASC, property_id ASC, value_id ASC;
"""


@dataclass
class ActiveStatementContribution:
    value_id: str
    entity_id: int
    property_id: int

    # CREATE or UPDATE timestamp that produced the current contribution.
    written_at: pd.Timestamp

    # Time value written at written_at.
    current_value_time: pd.Timestamp

    # The contribution is removed from the active average at this timestamp.
    expires_at: pd.Timestamp

    delay_days: float
    score: float

    # Eligible contributions are used in score, avg delay, and late count.
    is_eligible: bool
    is_late: bool

    # Eligible contribution whose time value lies after its write timestamp.
    is_future: bool

    # Too-old contributions are diagnostic only.
    is_too_old: bool

    # Used to ignore stale expiry-heap entries after UPDATE replacement.
    version: int


@dataclass
class AggregateTotals:
    score_sum: float = 0.0
    delay_sum: float = 0.0
    eligible_count: int = 0
    late_count: int = 0
    future_count: int = 0
    too_old_count: int = 0

    def add(self, contribution: ActiveStatementContribution) -> None:
        if contribution.is_eligible:
            self.score_sum += contribution.score
            self.delay_sum += contribution.delay_days
            self.eligible_count += 1

            if contribution.is_late:
                self.late_count += 1

            if contribution.is_future:
                self.future_count += 1

        if contribution.is_too_old:
            self.too_old_count += 1

    def remove(self, contribution: ActiveStatementContribution) -> None:
        if contribution.is_eligible:
            self.score_sum -= contribution.score
            self.delay_sum -= contribution.delay_days
            self.eligible_count -= 1

            if contribution.is_late:
                self.late_count -= 1

            if contribution.is_future:
                self.future_count -= 1

        if contribution.is_too_old:
            self.too_old_count -= 1

    def scaled_score(self) -> float | None:
        if self.eligible_count == 0:
            return None

        return self.score_sum / self.eligible_count

    def avg_delay_days(self) -> float | None:
        if self.eligible_count == 0:
            return None

        return self.delay_sum / self.eligible_count

    def is_empty(self) -> bool:
        return self.eligible_count == 0 and self.too_old_count == 0


class DataAgeAtInsertion(Metric):
    metric_name = "data_age_at_insertion"

    def __init__(self, datahandler: DataHandler, use_cache: bool):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []

        # Active latest-write contribution per statement.
        self.active_by_value_id: dict[str, ActiveStatementContribution] = {}

        # Property aggregates are maintained incrementally.
        self.property_totals: dict[int, AggregateTotals] = {}

        # Global statement-weighted aggregate.
        self.global_totals = AggregateTotals()

        # Expiry heap: (expires_at, value_id, contribution_version).
        self.expiry_heap: list[tuple[pd.Timestamp, str, int]] = []

        self._next_version = 0
        self._last_ts: Optional[pd.Timestamp] = None

        if use_cache:
            self._load_stateful_cache()

        # property_id -> (score, avg_delay, late_count, too_old_count,
        #                 statement_count, future_count)
        self._latest_result: dict[
            int,
            tuple[float | None, float | None, int, int, int, int],
        ] = {}
        self._latest_statement_average_score: float | None = None

    def calculate(self) -> None:
        property_df, statement_average_score = self.calculate_diff()
        self.write_result(property_df, statement_average_score)
        self.save_cache()

    def calculate_diff(self) -> tuple[pd.DataFrame, float | None]:
        df = self.datahandler.query_duckdb_df(
            DELTAS_SQL,
        )

        if df.empty:
            self._expire_until(self._current_timestep_ts())
            return self._snapshot_results()

        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
        raw_last_ts = df["timestamp"].max()

        df = df.dropna(subset=["timestamp"])
        if df.empty:
            self._advance_last_ts(raw_last_ts)
            self._expire_until(self._current_timestep_ts())
            return self._snapshot_results()

        for row in df.itertuples(index=False):
            timestamp = row.timestamp

            # Expire old contributions before handling later writes.
            self._expire_until(timestamp)

            value_id = str(row.value_id)
            entity_id = int(row.entity_id)
            property_id = int(row.property_id)
            action = str(row.action).upper()

            old_datatype_is_time = row.old_datatype == "time"
            new_datatype_is_time = row.new_datatype == "time"

            new_value_time = (
                parse_time_value(row.new_value)
                if new_datatype_is_time
                else None
            )

            if action == "CREATE":
                if new_datatype_is_time:
                    self._replace_contribution(
                        value_id=value_id,
                        entity_id=entity_id,
                        property_id=property_id,
                        written_at=timestamp,
                        value_time=new_value_time,
                    )

            elif action == "UPDATE":
                if new_datatype_is_time:
                    self._replace_contribution(
                        value_id=value_id,
                        entity_id=entity_id,
                        property_id=property_id,
                        written_at=timestamp,
                        value_time=new_value_time,
                    )
                elif old_datatype_is_time:
                    self._remove_contribution(value_id)

            elif action == "DELETE":
                if old_datatype_is_time:
                    self._remove_contribution(value_id)

        self._advance_last_ts(raw_last_ts)

        # Final expiry for the timestep snapshot.
        self._expire_until(self._current_timestep_ts())

        return self._snapshot_results()

    def write_result(
        self,
        property_df: pd.DataFrame,
        statement_average_score: float | None,
    ) -> None:
        self._write_property_result(property_df)
        self._write_global_statement_average(statement_average_score)

    def _write_property_result(self, df: pd.DataFrame) -> None:
        if df.empty:
            self._latest_result = {}
            return

        score_writer = DataHandler.SeriesWriter(
            self.datahandler,
            f"{self.metric_name}_scaled_score",
            MetricOnPropertyValueFloat,
        )
        delay_writer = DataHandler.SeriesWriter(
            self.datahandler,
            f"{self.metric_name}_avg_delay_days",
            MetricOnPropertyValueFloat,
        )
        late_writer = DataHandler.SeriesWriter(
            self.datahandler,
            f"{self.metric_name}_late_statement_count",
            MetricOnPropertyValueFloat,
        )
        too_old_writer = DataHandler.SeriesWriter(
            self.datahandler,
            f"{self.metric_name}_too_old_statement_count",
            MetricOnPropertyValueFloat,
        )
        statement_count_writer = DataHandler.SeriesWriter(
            self.datahandler,
            f"{self.metric_name}_statement_count",
            MetricOnPropertyValueFloat,
        )
        future_writer = DataHandler.SeriesWriter(
            self.datahandler,
            f"{self.metric_name}_future_statement_count",
            MetricOnPropertyValueFloat,
        )

        self._latest_result = {}

        for row in df.itertuples(index=False):
            property_id = int(row.property_id)

            score = None if pd.isna(row.scaled_score) else float(row.scaled_score)
            avg_delay = None if pd.isna(row.avg_delay_days) else float(row.avg_delay_days)
            late_count = int(row.late_statement_count)
            too_old_count = int(row.too_old_statement_count)
            statement_count = int(row.statement_count)
            future_count = int(row.future_statement_count)

            if score is not None:
                score_writer.add(property_id, score)

            if avg_delay is not None:
                delay_writer.add(property_id, avg_delay)

            late_writer.add(property_id, float(late_count))
            too_old_writer.add(property_id, float(too_old_count))
            statement_count_writer.add(property_id, float(statement_count))
            future_writer.add(property_id, float(future_count))

            self._latest_result[property_id] = (
                score,
                avg_delay,
                late_count,
                too_old_count,
                statement_count,
                future_count,
            )

        score_writer.write_all()
        delay_writer.write_all()
        late_writer.write_all()
        too_old_writer.write_all()
        statement_count_writer.write_all()
        future_writer.write_all()

    def _write_global_statement_average(self, value: float | None) -> None:
        self._latest_statement_average_score = value

        if value is None:
            return

        self.datahandler.write_global_metric_value(
            f"{self.metric_name}_statement_average_scaled_score",
            float(value),
        )


    def save_cache(self) -> None:
        recent_df = pd.DataFrame(
            [
                (
                    contribution.value_id,
                    contribution.entity_id,
                    contribution.property_id,
                    contribution.written_at,
                    contribution.current_value_time,
                    contribution.expires_at,
                    contribution.delay_days,
                    contribution.score,
                    contribution.is_eligible,
                    contribution.is_late,
                    contribution.is_future,
                    contribution.is_too_old,
                    contribution.version,
                )
                for contribution in self.active_by_value_id.values()
            ],
            columns=[
                "value_id",
                "entity_id",
                "property_id",
                "written_at",
                "current_value_time",
                "expires_at",
                "delay_days",
                "score",
                "is_eligible",
                "is_late",
                "is_future",
                "is_too_old",
                "version",
            ],
        )
        self.datahandler.save_cache_df(f"{self.metric_name}_recent", recent_df)

        meta_df = pd.DataFrame([{"last_ts": self._last_ts}])
        self.datahandler.save_cache_df(f"{self.metric_name}_meta", meta_df)

    def get_latest_result(
        self,
    ) -> Mapping[int, tuple[float | None, float | None, int, int, int, int]]:
        return self._latest_result

    def get_latest_statement_average_score(self) -> float | None:
        return self._latest_statement_average_score

    def log_memory(self) -> None:
        super().log_memory()
        log_memory_snapshot(
            self.metric_name,
            {
                "active_by_value_id": self.active_by_value_id,
                "property_totals": self.property_totals,
                "global_totals": self.global_totals,
                "latest_result": self._latest_result,
                "latest_statement_average_score": self._latest_statement_average_score,
            },
        )

    def _replace_contribution(
        self,
        value_id: str,
        entity_id: int,
        property_id: int,
        written_at: pd.Timestamp,
        value_time: pd.Timestamp | None,
    ) -> None:
        # UPDATE replaces the old contribution with a new write-time contribution.
        self._remove_contribution(value_id)

        contribution = self._make_contribution(
            value_id=value_id,
            entity_id=entity_id,
            property_id=property_id,
            written_at=written_at,
            value_time=value_time,
        )

        if contribution is None:
            return

        self._store_contribution(contribution)

    def _make_contribution(
        self,
        value_id: str,
        entity_id: int,
        property_id: int,
        written_at: pd.Timestamp,
        value_time: pd.Timestamp | None,
    ) -> ActiveStatementContribution | None:
        if value_time is None:
            return None

        # Historical values are outside the metric scope.
        if value_time < CUTOFFDATE:
            return None

        written_at = pd.to_datetime(written_at, utc=True)
        value_time = pd.to_datetime(value_time, utc=True)

        delay_days = self._delay_days(
            written_at=written_at,
            value_time=value_time,
        )

        is_eligible = delay_days <= MAX_DELAY_DAYS
        is_too_old = delay_days > MAX_DELAY_DAYS
        is_late = is_eligible and delay_days > GOAL_DAYS

        # Predicted events: the dated event lies after the write timestamp.
        # _delay_days clamps these to 0, so they score perfectly; count them
        # separately to make that part of the denominator visible.
        is_future = is_eligible and value_time > written_at

        score = 0.0
        if is_eligible:
            score = 1.0 - self._penalty(delay_days)

        version = self._next_version
        self._next_version += 1

        return ActiveStatementContribution(
            value_id=value_id,
            entity_id=entity_id,
            property_id=property_id,
            written_at=written_at,
            current_value_time=value_time,
            expires_at=written_at + timedelta(days=MAX_DELAY_DAYS),
            delay_days=delay_days,
            score=score,
            is_eligible=is_eligible,
            is_late=is_late,
            is_future=is_future,
            is_too_old=is_too_old,
            version=version,
        )

    def _store_contribution(self, contribution: ActiveStatementContribution) -> None:
        self.active_by_value_id[contribution.value_id] = contribution

        self.global_totals.add(contribution)

        property_total = self.property_totals.setdefault(
            contribution.property_id,
            AggregateTotals(),
        )
        property_total.add(contribution)

        heappush(
            self.expiry_heap,
            (
                contribution.expires_at,
                contribution.value_id,
                contribution.version,
            ),
        )

    def _remove_contribution(self, value_id: str) -> None:
        contribution = self.active_by_value_id.pop(value_id, None)

        if contribution is None:
            return

        self.global_totals.remove(contribution)

        property_total = self.property_totals.get(contribution.property_id)
        if property_total is None:
            return

        property_total.remove(contribution)

        if property_total.is_empty():
            del self.property_totals[contribution.property_id]

    def _expire_until(self, timestamp: pd.Timestamp | None) -> None:
        if timestamp is None or pd.isna(timestamp):
            return

        timestamp = pd.to_datetime(timestamp, utc=True)

        while self.expiry_heap:
            expires_at, value_id, version = self.expiry_heap[0]

            if expires_at > timestamp:
                break

            heappop(self.expiry_heap)

            contribution = self.active_by_value_id.get(value_id)
            if contribution is None:
                continue

            # UPDATE creates a new version; old heap entries become stale.
            if contribution.version != version:
                continue

            if contribution.expires_at <= timestamp:
                self._remove_contribution(value_id)

    def _snapshot_results(self) -> tuple[pd.DataFrame, float | None]:
        return (
            self._build_property_result(),
            self.global_totals.scaled_score(),
        )

    def _build_property_result(self) -> pd.DataFrame:
        if not self.property_totals:
            return self._empty_property_result()

        rows = []

        for property_id, total in self.property_totals.items():
            score = total.scaled_score()
            avg_delay = total.avg_delay_days()

            rows.append(
                {
                    "property_id": property_id,
                    "scaled_score": pd.NA if score is None else score,
                    "avg_delay_days": pd.NA if avg_delay is None else avg_delay,
                    "late_statement_count": total.late_count,
                    "too_old_statement_count": total.too_old_count,
                    "statement_count": total.eligible_count,
                    "future_statement_count": total.future_count,
                }
            )

        if not rows:
            return self._empty_property_result()

        return pd.DataFrame(rows)[
            [
                "property_id",
                "scaled_score",
                "avg_delay_days",
                "late_statement_count",
                "too_old_statement_count",
                "statement_count",
                "future_statement_count",
            ]
        ]

    def _load_stateful_cache(self) -> None:
        try:
            recent_df = self.datahandler.get_cache_df(f"{self.metric_name}_recent")
        except Exception:
            recent_df = pd.DataFrame()

        required_columns = {
            "value_id",
            "entity_id",
            "property_id",
            "written_at",
            "current_value_time",
            "expires_at",
            "delay_days",
            "score",
            "is_eligible",
            "is_late",
            "is_future",
            "is_too_old",
            "version",
        }

        if not recent_df.empty and not required_columns.issubset(recent_df.columns):
            missing = sorted(required_columns - set(recent_df.columns))
            raise ValueError(
                "data_age_at_insertion cache schema changed. "
                "Clear cache_data_age_at_insertion_recent and "
                "cache_data_age_at_insertion_meta before using this version. "
                f"Missing columns: {missing}"
            )

        for row in recent_df.itertuples(index=False):
            contribution = ActiveStatementContribution(
                value_id=str(row.value_id),
                entity_id=int(row.entity_id),
                property_id=int(row.property_id),
                written_at=pd.to_datetime(row.written_at, utc=True),
                current_value_time=pd.to_datetime(row.current_value_time, utc=True),
                expires_at=pd.to_datetime(row.expires_at, utc=True),
                delay_days=float(row.delay_days),
                score=float(row.score),
                is_eligible=bool(row.is_eligible),
                is_late=bool(row.is_late),
                is_future=bool(row.is_future),
                is_too_old=bool(row.is_too_old),
                version=int(row.version),
            )

            self._store_loaded_contribution(contribution)
            self._next_version = max(self._next_version, contribution.version + 1)

        try:
            meta_df = self.datahandler.get_cache_df(f"{self.metric_name}_meta")
            if not meta_df.empty:
                self._last_ts = pd.to_datetime(meta_df["last_ts"].iloc[0], utc=True)
        except Exception:
            self._last_ts = None

    def _store_loaded_contribution(
        self,
        contribution: ActiveStatementContribution,
    ) -> None:
        self.active_by_value_id[contribution.value_id] = contribution

        self.global_totals.add(contribution)

        property_total = self.property_totals.setdefault(
            contribution.property_id,
            AggregateTotals(),
        )
        property_total.add(contribution)

        heappush(
            self.expiry_heap,
            (
                contribution.expires_at,
                contribution.value_id,
                contribution.version,
            ),
        )

    def _advance_last_ts(self, timestamp: pd.Timestamp | None) -> None:
        if timestamp is None or pd.isna(timestamp):
            return

        timestamp = pd.to_datetime(timestamp, utc=True)

        if self._last_ts is None or timestamp > self._last_ts:
            self._last_ts = timestamp

    def _current_timestep_ts(self) -> pd.Timestamp | None:
        current_end = getattr(self.datahandler, "current_end_timestep", None)

        if current_end is None:
            return self._last_ts

        current_ts = pd.to_datetime(current_end, utc=True, errors="coerce")

        if pd.isna(current_ts):
            return self._last_ts

        return current_ts

    @staticmethod
    def _delay_days(
        written_at: pd.Timestamp,
        value_time: pd.Timestamp,
    ) -> float:
        delay = written_at - value_time

        if delay < timedelta(0):
            return 0.0

        return delay.total_seconds() / 86400.0

    @staticmethod
    def _penalty(delay_days: float) -> float:
        if delay_days <= GOAL_DAYS:
            return 0.0

        return (delay_days - GOAL_DAYS) / (MAX_DELAY_DAYS - GOAL_DAYS)

    @staticmethod
    def _empty_property_result() -> pd.DataFrame:
        return pd.DataFrame(
            columns=[
                "property_id",
                "scaled_score",
                "avg_delay_days",
                "late_statement_count",
                "too_old_statement_count",
                "statement_count",
                "future_statement_count",
            ]
        )