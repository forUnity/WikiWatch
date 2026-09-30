from __future__ import annotations

import os
import re
import subprocess
from collections import defaultdict
from collections.abc import Iterable

import pandas as pd
import psycopg2
from dotenv import load_dotenv
from psycopg2 import sql

from .model import DataKey, SeriesSpec


_CXNODE_RE = re.compile(r"^cx(\d+)$")


def _get_db_host() -> str:
    cxnode = subprocess.check_output(
        ["squeue", "--name=wikiwatch-db", "--noheader", "--format=%N"],
        stderr=subprocess.DEVNULL,
        text=True,
    ).strip()

    cxnode = cxnode.splitlines()[0].strip() if cxnode else ""
    match = _CXNODE_RE.fullmatch(cxnode)
    if match is None:
        raise RuntimeError(
            "Could not resolve the wikiwatch-db node. "
            f"Expected something like 'cx13', got {cxnode!r}."
        )

    return f"10.130.31.{int(match.group(1))}"


def _connect():
    load_dotenv(".env")
    host = os.environ.get("DB_HOST") or _get_db_host()

    required = {
        "DB_USER": os.environ.get("DB_USER"),
        "DB_PASS": os.environ.get("DB_PASS"),
        "DB_NAME": os.environ.get("DB_NAME"),
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing database settings: {', '.join(missing)}"
        )

    return psycopg2.connect(
        dbname=required["DB_NAME"],
        user=required["DB_USER"],
        password=required["DB_PASS"],
        host=host,
        port=os.environ.get("DB_PORT", "5432"),
        connect_timeout=30,
        gssencmode="disable",
    )


def _qualified_identifier(name: str) -> sql.Identifier:
    parts = name.split(".")
    if not parts or any(not part for part in parts):
        raise ValueError(f"Invalid table name: {name!r}")

    return sql.Identifier(*parts)


def fetch_series(
    specs: Iterable[SeriesSpec],
) -> dict[DataKey, pd.DataFrame]:
    """Fetch all requested plot series efficiently.

    Supported series:

    - ordinary:
      (run_id, metric_id)

    - property:
      (run_id, metric_id, property_id)

    - entity schema:
      (run_id, metric_id, entity_schema_id)

    - class:
      (run_id, metric_id, class_id)

    - aggregated property series:
      SUM/AVG over selected property IDs

    Results are stored using SeriesSpec.data_key instead of manually
    constructing DataKey tuples here. This keeps db.py aligned with model.py
    if the DataKey structure changes.
    """
    specs = tuple(specs)

    # Maps:
    #
    # SQL lookup tuple -> SeriesSpec.data_key
    #
    # This avoids rebuilding DataKey manually throughout db.py.
    ordinary_by_table: dict[
        str,
        dict[tuple[int, int], DataKey],
    ] = defaultdict(dict)

    property_by_table: dict[
        str,
        dict[tuple[int, int, int], DataKey],
    ] = defaultdict(dict)

    entity_schema_by_table: dict[
        str,
        dict[tuple[int, int, int], DataKey],
    ] = defaultdict(dict)

    class_by_table: dict[
        str,
        dict[tuple[int, int, int], DataKey],
    ] = defaultdict(dict)

    aggregated_specs: dict[DataKey, SeriesSpec] = {}

    for spec in specs:
        entity_schema_id = getattr(
            spec,
            "entity_schema_id",
            None,
        )

        class_id = getattr(
            spec,
            "class_id",
            None,
        )

        if entity_schema_id is not None:
            entity_schema_by_table[spec.table][
                (
                    spec.run_id,
                    spec.metric_id,
                    entity_schema_id,
                )
            ] = spec.data_key

        elif class_id is not None:
            class_by_table[spec.table][
                (
                    spec.run_id,
                    spec.metric_id,
                    class_id,
                )
            ] = spec.data_key

        elif spec.is_aggregated:
            aggregated_specs[spec.data_key] = spec

        elif spec.property_id is not None:
            property_by_table[spec.table][
                (
                    spec.run_id,
                    spec.metric_id,
                    spec.property_id,
                )
            ] = spec.data_key

        else:
            ordinary_by_table[spec.table][
                (
                    spec.run_id,
                    spec.metric_id,
                )
            ] = spec.data_key

    result: dict[DataKey, pd.DataFrame] = {}

    with _connect() as conn:

        # --------------------------------------------------------------
        # Ordinary metric_value_* series
        # --------------------------------------------------------------
        for table, requested in ordinary_by_table.items():
            ordered_pairs = sorted(requested)

            pair_sql = sql.SQL(", ").join(
                sql.SQL("(%s, %s)")
                for _ in ordered_pairs
            )

            query = sql.SQL(
                """
                SELECT
                    run_id,
                    metric_id,
                    timestamp,
                    value
                FROM {table}
                WHERE (run_id, metric_id) IN ({pairs})
                ORDER BY
                    run_id,
                    metric_id,
                    timestamp ASC
                """
            ).format(
                table=_qualified_identifier(table),
                pairs=pair_sql,
            )

            params = [
                item
                for pair in ordered_pairs
                for item in pair
            ]

            frame = pd.read_sql_query(
                query.as_string(conn),
                conn,
                params=params,
            )

            if not frame.empty:
                frame["timestamp"] = pd.to_datetime(
                    frame["timestamp"],
                    utc=True,
                )

            for run_id, metric_id in ordered_pairs:
                subset = frame[
                    (frame["run_id"] == run_id)
                    & (frame["metric_id"] == metric_id)
                ].copy()

                if subset.empty:
                    raise ValueError(
                        f"No data found for table={table!r}, "
                        f"run_id={run_id}, "
                        f"metric_id={metric_id}"
                    )

                data_key = requested[
                    (run_id, metric_id)
                ]

                result[data_key] = subset.sort_values(
                    "timestamp"
                )

        # --------------------------------------------------------------
        # Property-level series
        # --------------------------------------------------------------
        for table, requested in property_by_table.items():
            frames = _fetch_by_dimension(
                conn=conn,
                table=table,
                column="property_id",
                triples=set(requested),
            )

            for query_key, subset in frames.items():
                result[
                    requested[query_key]
                ] = subset

        # --------------------------------------------------------------
        # Entity-schema-level series
        #
        # No entity_schema_labels table is needed.
        # entity_schema_id is read directly from the metric table.
        # --------------------------------------------------------------
        for table, requested in entity_schema_by_table.items():
            frames = _fetch_by_dimension(
                conn=conn,
                table=table,
                column="entity_schema_id",
                triples=set(requested),
            )

            for query_key, subset in frames.items():
                result[
                    requested[query_key]
                ] = subset

        # --------------------------------------------------------------
        # Class-level series
        #
        # Kept so existing metric_on_class_* functionality is preserved.
        # --------------------------------------------------------------
        for table, requested in class_by_table.items():
            frames = _fetch_by_dimension(
                conn=conn,
                table=table,
                column="class_id",
                triples=set(requested),
            )

            for query_key, subset in frames.items():
                result[
                    requested[query_key]
                ] = subset

        # --------------------------------------------------------------
        # Aggregated property-level series
        # --------------------------------------------------------------
        for data_key, spec in aggregated_specs.items():
            if not isinstance(
                spec.property_id,
                tuple,
            ):
                raise ValueError(
                    "Aggregated series must contain "
                    "property_id as a tuple"
                )

            if spec.aggregation_method not in (
                "SUM",
                "AVG",
            ):
                raise ValueError(
                    "Aggregated series require "
                    "aggregation_method='SUM' or 'AVG'"
                )

            result[data_key] = _fetch_aggregated(
                conn=conn,
                table=spec.table,
                run_id=spec.run_id,
                metric_id=spec.metric_id,
                property_ids=spec.property_id,
                method=spec.aggregation_method,
            )

    return result


def _fetch_by_dimension(
    conn,
    table: str,
    column: str,
    triples: set[tuple[int, int, int]],
) -> dict[
    tuple[int, int, int],
    pd.DataFrame,
]:
    """Fetch series with one extra integer dimension.

    For example:

        property_id
        entity_schema_id
        class_id

    Query shape:

        (run_id, metric_id, dimension_id)
    """
    if not triples:
        return {}

    ordered_triples = sorted(triples)

    triple_sql = sql.SQL(", ").join(
        sql.SQL("(%s, %s, %s)")
        for _ in ordered_triples
    )

    query = sql.SQL(
        """
        SELECT
            run_id,
            metric_id,
            timestamp,
            {column},
            value
        FROM {table}
        WHERE (
            run_id,
            metric_id,
            {column}
        ) IN ({triples})
        ORDER BY
            run_id,
            metric_id,
            {column},
            timestamp ASC
        """
    ).format(
        column=sql.Identifier(column),
        table=_qualified_identifier(table),
        triples=triple_sql,
    )

    params = [
        item
        for triple in ordered_triples
        for item in triple
    ]

    frame = pd.read_sql_query(
        query.as_string(conn),
        conn,
        params=params,
    )

    if not frame.empty:
        frame["timestamp"] = pd.to_datetime(
            frame["timestamp"],
            utc=True,
        )

    result: dict[
        tuple[int, int, int],
        pd.DataFrame,
    ] = {}

    for (
        run_id,
        metric_id,
        dimension_id,
    ) in ordered_triples:

        subset = frame[
            (frame["run_id"] == run_id)
            & (frame["metric_id"] == metric_id)
            & (frame[column] == dimension_id)
        ].copy()

        if subset.empty:
            raise ValueError(
                f"No data found for table={table!r}, "
                f"run_id={run_id}, "
                f"metric_id={metric_id}, "
                f"{column}={dimension_id}"
            )

        result[
            (
                run_id,
                metric_id,
                dimension_id,
            )
        ] = subset.sort_values(
            "timestamp"
        )

    return result


def _fetch_aggregated(
    conn,
    table: str,
    run_id: int,
    metric_id: int,
    property_ids: tuple[int, ...],
    method: str,
) -> pd.DataFrame:
    """Aggregate a property metric per timestamp.

    ``method`` is SUM or AVG.

    If property_ids is empty, all properties belonging to the
    run/metric are included.
    """
    table_sql = _qualified_identifier(table)

    where = sql.SQL(
        "run_id = %s AND metric_id = %s"
    )

    params: list = [
        run_id,
        metric_id,
    ]

    if property_ids:
        where = sql.SQL(
            "{} AND property_id = ANY(%s)"
        ).format(where)

        params.append(
            list(property_ids)
        )

        # Check that every requested property actually exists.
        # Otherwise a typo could silently change the aggregate.
        check_query = sql.SQL(
            """
            SELECT DISTINCT property_id
            FROM {table}
            WHERE {where}
            """
        ).format(
            table=table_sql,
            where=where,
        )

        with conn.cursor() as cursor:
            cursor.execute(
                check_query,
                params,
            )

            found = {
                row[0]
                for row in cursor.fetchall()
            }

        missing = sorted(
            set(property_ids) - found
        )

        if missing:
            raise ValueError(
                f"No data found for table={table!r}, "
                f"run_id={run_id}, "
                f"metric_id={metric_id}, "
                f"property_ids={missing}"
            )

    # method is validated as SUM/AVG before arriving here.
    query = sql.SQL(
        """
        SELECT
            timestamp,
            {method}(value)::double precision AS value
        FROM {table}
        WHERE {where}
        GROUP BY timestamp
        ORDER BY timestamp ASC
        """
    ).format(
        method=sql.SQL(method),
        table=table_sql,
        where=where,
    )

    frame = pd.read_sql_query(
        query.as_string(conn),
        conn,
        params=params,
    )

    if frame.empty:
        raise ValueError(
            f"No data found for table={table!r}, "
            f"run_id={run_id}, "
            f"metric_id={metric_id}"
        )

    frame["timestamp"] = pd.to_datetime(
        frame["timestamp"],
        utc=True,
    )

    return frame