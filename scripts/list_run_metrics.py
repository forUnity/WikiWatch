"""List the metrics/statistics a run saved, with their metric ids.

Every metric is registered once in `metrics` (id, name, table_name); its values live in `table_name`
keyed by (run_id, metric_id, ...). A metric counts as saved when that table has at least one row for
the run.

Usage:
    python scripts/list_run_metrics.py --run-id 1800355
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


def saved_metrics(engine, run_id: int) -> list[tuple[int, str, str]]:
    with engine.connect() as conn:
        existing_tables = set(conn.execute(text(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()"
        )).scalars())
        value_tables = conn.execute(text("SELECT DISTINCT table_name FROM metrics")).scalars().all()

        rows = []
        for table in value_tables:
            if table not in existing_tables:
                continue
            # EXISTS per metric uses the (run_id, metric_id, ...) primary key instead of scanning the table
            rows += conn.execute(text(f"""
                SELECT m.id, m.name, m.table_name
                FROM metrics AS m
                WHERE m.table_name = :table
                  AND EXISTS (SELECT 1 FROM "{table}" AS v WHERE v.run_id = :run_id AND v.metric_id = m.id)
            """), {"table": table, "run_id": run_id}).all()
    return sorted(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", type=int, required=True)
    args = parser.parse_args()

    rows = saved_metrics(build_engine_read_only(), args.run_id)
    if not rows:
        print(f"No saved metrics found for run {args.run_id}")
        return

    name_width = max(len(name) for _, name, _ in rows)
    print(f"{'id':>6}  {'name':<{name_width}}  table")
    for metric_id, name, table in rows:
        print(f"{metric_id:>6}  {name:<{name_width}}  {table}")


if __name__ == "__main__":
    main()
