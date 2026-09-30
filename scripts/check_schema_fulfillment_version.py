"""Check which version of schema_fulfillment_count a run used for schema_fulfillment.

Each version is replayed from Wikidata's start up to --until from the database and compared against
the stored metric values:
  - old:     rows start as NULL (2026-07-06 to 174ebae), so only (entity, property) pairs with at least
             one edit are scored and entities without such an edit are left out of the average.
  - new:     rows start as (0, 0, min <= 0) for every entity in entity_type whose class maps to a schema
             (174ebae), whether or not the entity exists yet.
  - created: like new, but an entity only gets its rows once its creation row (action CREATE, target
             ENTITY in value_change or sitelink_change) is processed, and edits are deduplicated per
             schema row (SELECT DISTINCT). Edits of entities without a creation row (anomalies) only
             give rows for the edited properties.

Edits are replayed with the real SchemaFulfillmentCount.update_cache. Entities without any edit all score
the share of their optional properties; that part is aggregated in Postgres, because building rows for
every entity does not fit in local memory. The first timestep (default --until) has few edits, so the
replay is cheap; the population queries scan entity_type.

Usage:
    python scripts/check_schema_fulfillment_version.py
    python scripts/check_schema_fulfillment_version.py --runs 2551047 1814729 --until 2012-12-28T00:00:00+00:00
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
import os
import sys
import types
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

WIKIDATA_START = "2012-10-29T00:00:00+00:00"  # initial_time in metric_computation_scripts/main.py
TOLERANCE = 1e-6

# Same join as `query` in schema_fulfillment_count.py, restricted to the replayed time range. The old and
# new versions ran without DISTINCT, the created version with it.
VALUE_CHANGE_QUERY = """
SELECT {distinct} vc.revision_id, vc.value_id, vc.change_target,
       vc.entity_id, vc.property_id, vc.action, vc.old_value::text AS old_value, vc.new_value::text AS new_value,
       es.entity_schema_id
FROM value_change AS vc, entity_type AS et, entity_schemas AS es, schema_class_mapping AS scm
WHERE vc.entity_id = et.entity_id
AND es.entity_schema_id = scm.entity_schema_id
AND et.class_id = scm.class_id
AND vc.property_id = es.property_id
AND vc.timestamp >= :start AND vc.timestamp < :until
"""

# Cardinalities and value constraints of the schema properties, as SchemaFulfillmentCount.load_constraints reads them
CONSTRAINTS_QUERY = """
SELECT entity_schema_id, property_id, min, max, value_constraint FROM entity_schemas
"""

# Entities with a creation row in the replayed time range, like `query_created` in schema_fulfillment_count.py
CREATED_ENTITIES_QUERY = """
SELECT entity_id FROM value_change WHERE target = 'ENTITY' AND action = 'CREATE' AND timestamp >= :start AND timestamp < :until
UNION
SELECT entity_id FROM sitelink_change WHERE target = 'ENTITY' AND action = 'CREATE' AND timestamp >= :start AND timestamp < :until
"""

# Rows without values for the given entities, as the new version creates them at the start and the
# created version on creation
INIT_ROWS_QUERY = """
SELECT es.entity_schema_id, et.entity_id, es.property_id, COALESCE(es.min, 0) <= 0 AS is_within_min_max
FROM entity_type AS et, entity_schemas AS es, schema_class_mapping AS scm
WHERE es.entity_schema_id = scm.entity_schema_id
AND et.class_id = scm.class_id
AND et.entity_id = ANY(:entity_ids)
"""

# Score of every mapped entity (optionally only the created ones) if none of its properties had been
# edited: the share of (entity, property) pairs that are optional in all of the entity's schemas
DEFAULT_POPULATION_QUERY = """
SELECT count(*), sum(score)
FROM (
    SELECT entity_id, avg(CASE WHEN optional THEN 1.0 ELSE 0.0 END) AS score
    FROM (
        SELECT et.entity_id, es.property_id, bool_and(COALESCE(es.min, 0) <= 0) AS optional
        FROM entity_type AS et, entity_schemas AS es, schema_class_mapping AS scm
        WHERE es.entity_schema_id = scm.entity_schema_id
        AND et.class_id = scm.class_id
        {entity_filter}
        GROUP BY et.entity_id, es.property_id
    ) AS pairs
    GROUP BY entity_id
) AS entities
"""


def build_engine_read_only():
    load_dotenv(".env")
    settings = {name: os.environ.get(name) for name in ("DB_USER", "DB_PASS", "DB_NAME", "DB_HOST", "DB_PORT")}
    missing = [name for name, value in settings.items() if not value]
    if missing:
        raise RuntimeError(f"Missing DB env vars in .env: {', '.join(missing)}")

    return create_engine(
        f"postgresql+psycopg2://{settings['DB_USER']}:{settings['DB_PASS']}@{settings['DB_HOST']}:{settings['DB_PORT']}/{settings['DB_NAME']}",
        connect_args={"options": "-c default_transaction_read_only=on"},
    )


def load_schema_fulfillment_count_class():
    """Load SchemaFulfillmentCount without its pipeline dependencies (datahandler needs SLURM and
    the Unix-only `resource` module), and without clashing with the stdlib `statistics` package."""
    for name in ("metric", "datahandler", "utils", "utils.memory_tracker"):
        sys.modules.setdefault(name, types.ModuleType(name))
    sys.modules["metric"].Metric = getattr(sys.modules["metric"], "Metric", type("Metric", (), {}))
    sys.modules["datahandler"].DataHandler = getattr(sys.modules["datahandler"], "DataHandler", object)
    sys.modules["utils.memory_tracker"].log_memory_snapshot = lambda *args, **kwargs: None

    path = Path(__file__).resolve().parents[1] / "metric_computation_scripts" / "statistics" / "schema_fulfillment_count.py"
    spec = importlib.util.spec_from_file_location("schema_fulfillment_count_replay", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SchemaFulfillmentCount


def replay(sfc_class, cache: dict, value_changes: pd.DataFrame, constraints: dict) -> dict:
    sfc = sfc_class.__new__(sfc_class)
    sfc.cache = cache
    sfc.changed_rows = {}
    sfc.constraints = constraints
    sfc.compliance_cache = {}
    # Its warning about edits without rows does not apply to the replays, which start from partial caches
    with contextlib.redirect_stdout(io.StringIO()):
        sfc.update_cache(value_changes)
    return sfc.cache


def load_constraints(sfc_class, conn) -> dict:
    """The constraints dict that update_cache looks the schema properties up in, read like the real one."""
    sfc = sfc_class.__new__(sfc_class)
    sfc.datahandler = types.SimpleNamespace(query_duckdb_df=lambda query: pd.read_sql(text(CONSTRAINTS_QUERY), conn))
    with contextlib.redirect_stdout(io.StringIO()):
        return sfc.load_constraints()


def entity_scores(cache: dict) -> dict[int, float]:
    """Per-entity score as in SchemaFulfillment: the fraction of complete properties among the counted ones."""
    groups: dict[tuple[int, int], bool] = {}
    for (_, entity_id, property_id), (num_compliant, num_noncompliant, is_within_min_max) in cache.items():
        if pd.isna(num_compliant) or pd.isna(num_noncompliant) or pd.isna(is_within_min_max):
            continue
        compliant = num_noncompliant == 0 and bool(is_within_min_max)
        groups[(entity_id, property_id)] = groups.get((entity_id, property_id), True) and compliant

    counts: dict[int, tuple[int, int]] = defaultdict(lambda: (0, 0))
    for (entity_id, _), compliant in groups.items():
        compliant_count, total_count = counts[entity_id]
        counts[entity_id] = (compliant_count + compliant, total_count + 1)
    return {entity_id: c / t for entity_id, (c, t) in counts.items()}


def default_population(conn, params: dict, created_only: bool) -> tuple[int, float]:
    entity_filter = f"AND et.entity_id IN ({CREATED_ENTITIES_QUERY})" if created_only else ""
    query = text(DEFAULT_POPULATION_QUERY.format(entity_filter=entity_filter))
    count, score_sum = conn.execute(query, params if created_only else {}).one()
    return int(count), float(score_sum or 0)


def stored_values(conn, runs: list[int], until: datetime) -> dict[int, float | None]:
    metric = conn.execute(text("SELECT id, table_name FROM metrics WHERE name = 'schema_fulfillment'")).one_or_none()
    if metric is None:
        raise RuntimeError("Metric schema_fulfillment is not registered in `metrics`")
    metric_id, table = metric
    values = {}
    for run in runs:
        values[run] = conn.execute(text(f"""
            SELECT value FROM "{table}" WHERE run_id = :run_id AND metric_id = :metric_id AND timestamp = :until
        """), {"run_id": run, "metric_id": metric_id, "until": until}).scalar_one_or_none()
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--runs", type=int, nargs="+", default=[2551047, 1814729])
    parser.add_argument("--until", type=datetime.fromisoformat, default=datetime.fromisoformat("2012-11-28T00:00:00+00:00"),
                        help="End of the timestep to check (exclusive, as in load_next_timestep). Default: first timestep.")
    args = parser.parse_args()
    params = {"start": datetime.fromisoformat(WIKIDATA_START), "until": args.until}
    sfc_class = load_schema_fulfillment_count_class()

    with build_engine_read_only().connect() as conn:
        print(f"Replaying schema edits {params['start']} -> {args.until} ...")
        value_changes = pd.read_sql(text(VALUE_CHANGE_QUERY.format(distinct="")), conn, params=params)
        distinct_changes = pd.read_sql(text(VALUE_CHANGE_QUERY.format(distinct="DISTINCT")), conn, params=params)
        edited_entities = sorted({int(e) for e in value_changes["entity_id"]})
        created_entities = {int(e) for e in conn.execute(text(CREATED_ENTITIES_QUERY), params).scalars()}
        anomalies = [e for e in edited_entities if e not in created_entities]
        print(f"  {len(value_changes)} schema edits on {len(edited_entities)} entities, "
              f"{len(value_changes) - len(distinct_changes)} of them duplicated by the join")
        print(f"  {len(created_entities)} created entities, {len(anomalies)} edited entities without a creation row")

        constraints = load_constraints(sfc_class, conn)

        # Old version: NULL rows behave exactly like missing rows, so an empty cache is equivalent
        old_scores = entity_scores(replay(sfc_class, {}, value_changes, constraints))

        # New and created version: rows without values for the edited entities (created ones only for
        # the created version) before replaying the edits
        init_rows = pd.read_sql(text(INIT_ROWS_QUERY), conn, params={"entity_ids": edited_entities})
        init_cache = {
            (int(r.entity_schema_id), int(r.entity_id), int(r.property_id)): (0, 0, bool(r.is_within_min_max))
            for r in init_rows.itertuples()
        }
        created_init_cache = {key: row for key, row in init_cache.items() if key[1] in created_entities}
        new_edited_default_sum = sum(entity_scores(init_cache).values())
        created_edited_default_sum = sum(entity_scores(created_init_cache).values())
        new_edited_scores = entity_scores(replay(sfc_class, dict(init_cache), value_changes, constraints))
        created_edited_scores = entity_scores(replay(sfc_class, dict(created_init_cache), distinct_changes, constraints))

        print("Aggregating never-edited entities in Postgres (scans entity_type) ...")
        new_count, new_default_sum = default_population(conn, params, created_only=False)
        created_count, created_default_sum = default_population(conn, params, created_only=True)

        stored = stored_values(conn, args.runs, args.until)

    expected = {
        "old": sum(old_scores.values()) / len(old_scores) if old_scores else None,
        "new": (new_default_sum - new_edited_default_sum + sum(new_edited_scores.values())) / new_count if new_count else None,
        # Anomalies are not in the created population, but get partial rows through their edits
        "created": ((created_default_sum - created_edited_default_sum + sum(created_edited_scores.values()))
                    / (created_count + len(anomalies)) if created_count + len(anomalies) else None),
    }

    print()
    print(f"entities averaged: old {len(old_scores)}, new {new_count}, created {created_count + len(anomalies)}")
    print(f"expected value at {args.until}: " + ", ".join(f"{name} {value}" for name, value in expected.items()))
    for run, value in stored.items():
        if value is None:
            print(f"run {run}: no schema_fulfillment value at {args.until}")
            continue
        matches = [name for name, want in expected.items() if want is not None and abs(value - want) < TOLERANCE]
        verdict = f"matches the {' and '.join(matches)} version" if matches else "matches no version"
        print(f"run {run}: stored {value:.8f} -> {verdict}")


if __name__ == "__main__":
    main()
