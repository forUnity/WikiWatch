"""Compare the schema fulfillment results of two runs per timestep.

Defaults compare run 2551047 (new implementation, aborted) against run 1814729 (old implementation)
on metric 90 (schema_fulfillment) and metric 91 (schema_fulfillment_optional). Only timesteps that
exist in both runs are compared; diff = new - old.

Usage:
    python Scripts/compare_schema_fulfillment_runs.py
    python Scripts/compare_schema_fulfillment_runs.py --new-run 2551047 --old-run 1814729 --metric-ids 90 91
"""

from __future__ import annotations

import argparse
import os

from dotenv import load_dotenv
from sqlalchemy import create_engine, text


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


def metric_values(conn, table: str, run_id: int, metric_id: int) -> dict:
    rows = conn.execute(text(f"""
        SELECT timestamp, value FROM "{table}"
        WHERE run_id = :run_id AND metric_id = :metric_id
    """), {"run_id": run_id, "metric_id": metric_id}).all()
    return dict(rows)


def compare_metric(conn, metric_id: int, new_run: int, old_run: int) -> None:
    metric = conn.execute(text("SELECT name, table_name FROM metrics WHERE id = :id"), {"id": metric_id}).one_or_none()
    if metric is None:
        print(f"Metric {metric_id} is not registered in `metrics`\n")
        return
    name, table = metric

    new_values = metric_values(conn, table, new_run, metric_id)
    old_values = metric_values(conn, table, old_run, metric_id)
    shared = sorted(new_values.keys() & old_values.keys())

    print(f"=== metric {metric_id} ({name}) ===")
    print(f"timesteps: run {new_run}={len(new_values)}, run {old_run}={len(old_values)}, shared={len(shared)}")
    if not shared:
        print()
        return

    print(f"{'timestamp':<27}  {f'run {new_run}':>14}  {f'run {old_run}':>14}  {'diff':>12}  {'rel diff':>9}")
    diffs = []
    for ts in shared:
        new, old = new_values[ts], old_values[ts]
        diff = new - old
        diffs.append(diff)
        rel = f"{diff / old:>+9.2%}" if old else f"{'n/a':>9}"
        print(f"{ts.isoformat(sep=' ', timespec='seconds'):<27}  {new:>14.8f}  {old:>14.8f}  {diff:>+12.8f}  {rel}")

    abs_diffs = [abs(d) for d in diffs]
    print(f"mean diff: {sum(diffs) / len(diffs):+.8f}   mean |diff|: {sum(abs_diffs) / len(abs_diffs):.8f}   "
          f"max |diff|: {max(abs_diffs):.8f}   last diff: {diffs[-1]:+.8f}\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--new-run", type=int, default=2551047)
    parser.add_argument("--old-run", type=int, default=1814729)
    parser.add_argument("--metric-ids", type=int, nargs="+", default=[90, 91])
    args = parser.parse_args()

    with build_engine_read_only().connect() as conn:
        for metric_id in args.metric_ids:
            compare_metric(conn, metric_id, args.new_run, args.old_run)


if __name__ == "__main__":
    main()
