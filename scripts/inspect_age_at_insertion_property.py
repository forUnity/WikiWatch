"""Print the future / current / late statement counts of the data age at insertion metric for one
property, per timestep of a run.

The counts are read from the stored per-property results of DataAgeAtInsertion
(metrics/data_age_at_insertion_per_prop.py):
    total   = data_age_at_insertion_statement_count          (eligible statements, the score denominator)
    late    = data_age_at_insertion_late_statement_count     (delay > GOAL_DAYS)
    future  = data_age_at_insertion_future_statement_count   (time value after the write, delay clamped to 0)
    current = total - late - future                          (time value at most GOAL_DAYS before the write)
    too old = data_age_at_insertion_too_old_statement_count  (delay > MAX_DELAY_DAYS, not part of total)
    eligible = total / (total + too old)                     (share of the statements the score uses)

These are not edits made within the timestep: a statement contributes from its latest CREATE/UPDATE
until MAX_DELAY_DAYS later, so each timestep counts the statements last written in the trailing
MAX_DELAY_DAYS window. The metric writes no row for a property without active statements; those
timesteps are printed as 0.

--list-all prints only the sum over timesteps and the mean per timestep, for every property the run
wrote results for, ordered by total DESC.

Usage:
    python scripts/inspect_age_at_insertion_property.py --property P570
    python scripts/inspect_age_at_insertion_property.py --property 570 --run-id 2551047
    python scripts/inspect_age_at_insertion_property.py --list-all
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

from list_written_properties import read_names_csv

METRIC_PREFIX = "data_age_at_insertion"
COUNT_METRICS = {
    "total": f"{METRIC_PREFIX}_statement_count",
    "late": f"{METRIC_PREFIX}_late_statement_count",
    "future": f"{METRIC_PREFIX}_future_statement_count",
    "too_old": f"{METRIC_PREFIX}_too_old_statement_count",
}
COLUMNS = ("future", "current", "late", "total", "too_old")
LABEL_WIDTH = 30


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


def parse_property_id(value: str) -> int:
    value = value.strip().upper()
    return int(value[1:] if value.startswith("P") else value)


def metric_ids(conn) -> dict[str, tuple[int, str]]:
    """key -> (metric id, value table) for the metrics in COUNT_METRICS."""
    rows = conn.execute(
        text("SELECT name, id, table_name FROM metrics WHERE name = ANY(:names)"),
        {"names": list(COUNT_METRICS.values())},
    ).all()
    by_name = {name: (metric_id, table) for name, metric_id, table in rows}

    missing = [name for name in COUNT_METRICS.values() if name not in by_name]
    if missing:
        raise RuntimeError(f"Metrics not registered in `metrics`: {', '.join(missing)}")
    return {key: by_name[name] for key, name in COUNT_METRICS.items()}


def latest_run(conn, metric_id: int, table: str) -> int | None:
    # EXISTS per run uses the (run_id, metric_id, ...) primary key instead of scanning the table
    return conn.execute(text(f"""
        SELECT r.id FROM runs AS r
        WHERE EXISTS (SELECT 1 FROM "{table}" AS v WHERE v.run_id = r.id AND v.metric_id = :metric_id)
        ORDER BY r.created_at DESC, r.id DESC
        LIMIT 1
    """), {"metric_id": metric_id}).scalar_one_or_none()


def timesteps(conn, run_id: int, metric_id: int, table: str) -> list:
    """Every timestep the metric wrote results for, for any property."""
    return conn.execute(text(f"""
        SELECT DISTINCT timestamp FROM "{table}"
        WHERE run_id = :run_id AND metric_id = :metric_id
        ORDER BY timestamp
    """), {"run_id": run_id, "metric_id": metric_id}).scalars().all()


def property_values(conn, run_id: int, metric_id: int, table: str, property_id: int) -> dict:
    rows = conn.execute(text(f"""
        SELECT timestamp, value FROM "{table}"
        WHERE run_id = :run_id AND metric_id = :metric_id AND property_id = :property_id
    """), {"run_id": run_id, "metric_id": metric_id, "property_id": property_id}).all()
    return dict(rows)


def property_sums(conn, run_id: int, metric_id: int, table: str) -> dict[int, int]:
    """property id -> value summed over all timesteps."""
    rows = conn.execute(text(f"""
        SELECT property_id, SUM(value) FROM "{table}"
        WHERE run_id = :run_id AND metric_id = :metric_id
        GROUP BY property_id
    """), {"run_id": run_id, "metric_id": metric_id}).all()
    return {property_id: int(value) for property_id, value in rows}


def eligible_share(counts: dict) -> str:
    """total / (total + too old): share of the statements written in the window that the score uses."""
    written = counts["total"] + counts["too_old"]
    return f"{counts['total'] / written:.1%}" if written else "n/a"


def sum_columns(sums: dict) -> str:
    return "  ".join(f"{sums[key]:>8}" for key in COLUMNS) + f"  {eligible_share(sums):>8}"


def mean_columns(sums: dict, step_count: int) -> str:
    return "  ".join(f"{sums[key] / step_count:>8.1f}" for key in COLUMNS)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--property", help="property id, e.g. P570 or 570")
    target.add_argument("--list-all", action="store_true",
                        help="sum and mean rows for every property of the run, ordered by total DESC")
    parser.add_argument("--run-id", type=int, help="defaults to the most recent run that saved the metric")
    args = parser.parse_args()
    property_id = None if args.list_all else parse_property_id(args.property)

    with build_engine_read_only().connect() as conn:
        metrics = metric_ids(conn)
        total_id, table = metrics["total"]

        run_id = args.run_id if args.run_id is not None else latest_run(conn, total_id, table)
        if run_id is None:
            print(f"No run saved {COUNT_METRICS['total']}")
            return

        steps = timesteps(conn, run_id, total_id, table)
        if not steps:
            print(f"Run {run_id} saved no {COUNT_METRICS['total']} values")
            return

        if args.list_all:
            sums_by_key = {
                key: property_sums(conn, run_id, metric_id, metric_table)
                for key, (metric_id, metric_table) in metrics.items()
            }
            print_all_properties(run_id, steps, sums_by_key)
            return

        values = {
            key: property_values(conn, run_id, metric_id, metric_table, property_id)
            for key, (metric_id, metric_table) in metrics.items()
        }

    print_property(run_id, steps, property_id, values)


def print_property(run_id: int, steps: list, property_id: int, values: dict) -> None:
    # Labels come from the CSV list_written_properties.py fills, not the database: property_labels
    # only exists where the dump was imported, not where runs were moved with run_transfer.py
    label = read_names_csv().get(property_id)
    print(f"run {run_id}, property P{property_id} ({label or 'no label'}), {len(steps)} timesteps")
    print(f"{'timestamp':<27}  {'future':>8}  {'current':>8}  {'late':>8}  {'total':>8}  {'too old':>8}  {'eligible':>8}")

    sums = dict.fromkeys(COLUMNS, 0)
    for ts in steps:
        row = {key: int(values[key].get(ts, 0)) for key in COUNT_METRICS}
        row["current"] = row["total"] - row["late"] - row["future"]
        for key in sums:
            sums[key] += row[key]
        print(f"{ts.isoformat(sep=' ', timespec='seconds'):<27}  {row['future']:>8}  {row['current']:>8}  "
              f"{row['late']:>8}  {row['total']:>8}  {row['too_old']:>8}  {eligible_share(row):>8}")

    print(f"{'sum over timesteps':<27}  {sum_columns(sums)}")
    print(f"{'mean per timestep':<27}  {mean_columns(sums, len(steps))}")
    if sums["total"]:
        print(f"share of total: future {sums['future'] / sums['total']:.1%}, "
              f"current {sums['current'] / sums['total']:.1%}, late {sums['late'] / sums['total']:.1%}")


def print_all_properties(run_id: int, steps: list, sums_by_key: dict[str, dict[int, int]]) -> None:
    property_ids = set().union(*sums_by_key.values())
    rows = []
    for property_id in property_ids:
        sums = {key: sums_by_key[key].get(property_id, 0) for key in COUNT_METRICS}
        sums["current"] = sums["total"] - sums["late"] - sums["future"]
        rows.append((property_id, sums))
    rows.sort(key=lambda row: (-row[1]["total"], row[0]))

    labels = read_names_csv()
    print(f"run {run_id}, {len(rows)} properties, {len(steps)} timesteps")
    print(f"{'property':<9}  {'label':<{LABEL_WIDTH}}  {'':<4}  {'future':>8}  {'current':>8}  {'late':>8}  "
          f"{'total':>8}  {'too old':>8}  {'eligible':>8}")
    for property_id, sums in rows:
        label = (labels.get(property_id) or "no label")[:LABEL_WIDTH]
        print(f"{f'P{property_id}':<9}  {label:<{LABEL_WIDTH}}  {'sum':<4}  {sum_columns(sums)}")
        print(f"{'':<9}  {'':<{LABEL_WIDTH}}  {'mean':<4}  {mean_columns(sums, len(steps))}")


if __name__ == "__main__":
    main()
