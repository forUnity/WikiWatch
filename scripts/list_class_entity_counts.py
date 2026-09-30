"""List the classes of a per-class count metric in a run, largest count first, with their Wikidata labels.

Defaults to `completeness_currency_entity_count_per_class` (metric_on_class_value_float): the number of
entities per class that are in the completeness currency averages. The counts are the values at one
timestamp, by default the last one the metric wrote in the run.

Labels come from `class_names.csv`, which `list_written_classes.py` fills. To label every class:
    python scripts/list_written_classes.py --run-id 2560932 --metric-id 225   (cluster)
    python scripts/list_written_classes.py --query-class-names                (locally)

Usage:
    python scripts/list_class_entity_counts.py --run-id 2560932
    python scripts/list_class_entity_counts.py --run-id 2560932 --timestamp 2020-05-20 --limit 50
"""

from __future__ import annotations

import argparse

from list_written_classes import read_names_csv

DEFAULT_METRIC = "completeness_currency_entity_count_per_class"


def class_counts(engine, run_id: int, metric_name: str, timestamp: str | None) -> tuple[int, str, object, list[tuple[int, float]]]:
    """Returns (metric_id, table, timestamp, [(class_id, count), ...]) with the counts in descending order."""
    from sqlalchemy import text

    with engine.connect() as conn:
        metric = conn.execute(text("SELECT id, table_name FROM metrics WHERE name = :name"), {"name": metric_name}).first()
        if metric is None:
            raise SystemExit(f"Metric {metric_name!r} does not exist")
        metric_id, table = metric.id, metric.table_name

        # Latest timestamp at or before the requested one, so any date inside a timestep works
        at = conn.execute(text(f"""
            SELECT MAX(timestamp) FROM "{table}"
            WHERE run_id = :run_id AND metric_id = :metric_id
            {"AND timestamp <= CAST(:timestamp AS timestamptz)" if timestamp else ""}
        """), {"run_id": run_id, "metric_id": metric_id, "timestamp": timestamp}).scalar()
        if at is None:
            raise SystemExit(f"Metric {metric_id} ({metric_name}) has no values in run {run_id}"
                             + (f" at or before {timestamp}" if timestamp else ""))

        rows = conn.execute(text(f"""
            SELECT class_id, value FROM "{table}"
            WHERE run_id = :run_id AND metric_id = :metric_id AND timestamp = :at
            ORDER BY value DESC, class_id
        """), {"run_id": run_id, "metric_id": metric_id, "at": at}).all()

    return metric_id, table, at, [(class_id, value) for class_id, value in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", type=int, required=True)
    parser.add_argument("--metric-name", default=DEFAULT_METRIC)
    parser.add_argument("--timestamp", help="use the last values at or before this time (default: the last timestamp of the run)")
    parser.add_argument("--limit", type=int, help="print only the first N classes")
    args = parser.parse_args()

    from list_run_metrics import build_engine_read_only

    metric_id, table, at, rows = class_counts(build_engine_read_only(), args.run_id, args.metric_name, args.timestamp)
    names = read_names_csv()
    total = sum(value for _, value in rows)

    print(f"metric {metric_id} ({args.metric_name}) in {table}, run {args.run_id}, at {at}: "
          f"{len(rows)} classes, {total:,.0f} in total (an entity counts once for each of its classes)")
    print(f"{'#':>5}  {'class':>12}  {'count':>12}  {'share':>6}  label")
    for rank, (class_id, value) in enumerate(rows[:args.limit], start=1):
        label = names.get(class_id)
        label = "?" if label is None else label or "-"
        share = value / total if total else 0.0
        print(f"{rank:>5}  {'Q' + str(class_id):>12}  {value:>12,.0f}  {share:>6.1%}  {label}")

    missing = sum(class_id not in names for class_id, _ in rows)
    if missing:
        print(f"\n{missing} label(s) unknown (?): see the docstring for filling class_names.csv.")


if __name__ == "__main__":
    main()
