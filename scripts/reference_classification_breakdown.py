"""Re-slice the flow classification of a run using only what the run already logged.

Two analyses, both per timestep:

1. Class mass fractions with some domains excluded.
   Each domain's class depends only on its own in/out weight and mass, so excluding a domain X
   only removes its mass from its class and from the denominator:

       f'_c = (f_c * M - sum of m_X over excluded X in class c) / (M - sum of m_X over all excluded X)

   M is the mass the classifier divides by (sum of reference_counts_per_domain, which equals
   total_reference_count - tail_count). This means "X is left out of the evaluation", not "X never
   existed": the preferences X took part in would also change the weights of other domains, and
   those weights are not logged.

2. Number of domains per class among the largest domains.
   The classifier writes the class of only the `top_largest_domains_to_write` (default 1000)
   largest domains by mass, so counts are exact for those and unknown beyond them. The mass those
   domains hold per class is reported next to the counts, to show how much of each class they cover.

Only the flow classifiers (flow_classification_supermajority, flow_classification_wilson) are
supported: the older graph classifier drops unclassified domains from its per-domain output, so the
top-1000 set cannot be reconstructed from it.

Usage:
    python scripts/reference_classification_breakdown.py
    python scripts/reference_classification_breakdown.py --run-id 2559236 --exclude wikipedia.org Q42

The fractions CSV is named after the excluded domains (class_fractions_without_<a>+<b>_run<id>.csv),
so runs with different exclusions write separate files.
"""

from __future__ import annotations

import argparse
import logging
import os
import re

import numpy as np
import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import create_engine, text

log = logging.getLogger(__name__)

DEFAULT_RUN_ID = 2559236

MASS_METRIC = "reference_counts_per_domain"
TOTAL_REFERENCE_COUNT_METRIC = "reference_counts_per_domain_total_reference_count"
TAIL_COUNT_METRIC = "reference_counts_per_domain_tail_count"
CLASSIFICATION_SUFFIX = "_domain_classification"

# Same encoding as CLASSIFICATION_WRITE_VALUES in the flow classifiers.
CLASS_OF_VALUE = {-1.0: "dominated", 0.0: "contested", 1.0: "dominating", 2.0: "unclassified"}
CLASSES = ("dominated", "dominating", "contested", "unclassified")


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


def metric_ids(engine, names: list[str]) -> dict[str, int]:
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT name, id FROM metrics WHERE name = ANY(:names)"), {"names": names}
        ).all()
    found = {name: metric_id for name, metric_id in rows}
    missing = [name for name in names if name not in found]
    if missing:
        raise RuntimeError(f"Metrics not registered in the database: {missing}")
    return found


def detect_classifier(engine, run_id: int) -> str:
    """The metric prefix of the flow classifier that wrote a per-domain classification in this run."""
    sql = r"""
    SELECT m.name
    FROM metrics AS m
    WHERE m.name LIKE 'flow\_classification%\_domain\_classification'
      AND EXISTS (SELECT 1 FROM metric_on_string AS v WHERE v.run_id = :run_id AND v.metric_id = m.id)
    """
    with engine.connect() as connection:
        names = connection.execute(text(sql), {"run_id": run_id}).scalars().all()
    if len(names) != 1:
        raise RuntimeError(
            f"Expected exactly one flow classifier in run {run_id}, found {names}. Pass --classifier."
        )
    return names[0][: -len(CLASSIFICATION_SUFFIX)]


def load_global_series(engine, run_id: int, names: list[str]) -> pd.DataFrame:
    """One row per timestep, one column per global metric."""
    sql = """
    SELECT m.name, v.timestamp, v.value
    FROM metric_value_float AS v
    JOIN metrics AS m ON m.id = v.metric_id
    WHERE v.run_id = :run_id AND m.name = ANY(:names)
    """
    frame = pd.read_sql_query(text(sql), engine, params={"run_id": run_id, "names": names})
    missing = sorted(set(names) - set(frame["name"]))
    if missing:
        raise RuntimeError(f"Run {run_id} has no values for {missing}")
    # metric_value_float.timestamp is timestamptz while metric_on_string.timestamp is not; both
    # are brought to naive timestamps and matched by nearest timestep below.
    frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True).dt.tz_convert(None)
    return frame.pivot_table(index="timestamp", columns="name", values="value", aggfunc="last").reset_index()


def load_top_domain_classes(engine, run_id: int, classification_id: int, mass_id: int) -> pd.DataFrame:
    """Every (timestamp, domain) the classifier wrote a class for, with that domain's mass."""
    sql = """
    SELECT c.timestamp, c.key_string AS domain, c.value AS class_value, r.value AS mass
    FROM metric_on_string AS c
    LEFT JOIN metric_on_string AS r
      ON r.run_id = c.run_id AND r.metric_id = :mass_id
     AND r.timestamp = c.timestamp AND r.key_string = c.key_string
    WHERE c.run_id = :run_id AND c.metric_id = :classification_id
    """
    params = {"run_id": run_id, "classification_id": classification_id, "mass_id": mass_id}
    frame = pd.read_sql_query(text(sql), engine, params=params)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    frame["class"] = frame["class_value"].map(CLASS_OF_VALUE)
    unknown = frame.loc[frame["class"].isna(), "class_value"].unique()
    if len(unknown):
        raise RuntimeError(f"Unexpected classification values {unknown}; is this a flow classifier?")
    return frame


def load_domain_mass(engine, run_id: int, mass_id: int, timestamps: pd.Series, domains: list[str]) -> pd.DataFrame:
    """Mass of the given domains at the given timestamps.

    Built as one primary-key lookup per (timestamp, domain) rather than a filter on key_string,
    which would scan every mass row of the run.
    """
    sql = """
    SELECT t.ts AS timestamp, d.domain, r.value AS mass
    FROM unnest(CAST(:timestamps AS timestamp[])) AS t(ts)
    CROSS JOIN unnest(CAST(:domains AS text[])) AS d(domain)
    JOIN metric_on_string AS r
      ON r.run_id = :run_id AND r.metric_id = :mass_id
     AND r.timestamp = t.ts AND r.key_string = d.domain
    """
    params = {
        "run_id": run_id,
        "mass_id": mass_id,
        "timestamps": [ts.to_pydatetime() for ts in timestamps],
        "domains": domains,
    }
    frame = pd.read_sql_query(text(sql), engine, params=params)
    frame["timestamp"] = pd.to_datetime(frame["timestamp"])
    return frame


def attach_globals(per_timestep: pd.DataFrame, global_series: pd.DataFrame) -> pd.DataFrame:
    """Match each timestep to the global values written in it (nearest within a day)."""
    left = per_timestep.assign(timestamp=per_timestep["timestamp"].astype("datetime64[ns]")).sort_values("timestamp")
    right = global_series.assign(timestamp=global_series["timestamp"].astype("datetime64[ns]")).sort_values("timestamp")
    merged = pd.merge_asof(
        left, right, on="timestamp", direction="nearest", tolerance=pd.Timedelta(days=1)
    )
    unmatched = merged[right.columns.drop("timestamp")].isna().all(axis=1).sum()
    if unmatched:
        log.warning("%d timesteps have no global values within a day.", unmatched)
    return merged


def fractions_without(
    top_classes: pd.DataFrame,
    excluded_mass: pd.DataFrame,
    global_series: pd.DataFrame,
    classifier: str,
    excluded: list[str],
    top_size: int,
) -> pd.DataFrame:
    timestamps = pd.DataFrame({"timestamp": np.sort(top_classes["timestamp"].unique())})
    frame = attach_globals(timestamps, global_series)
    frame["denominator"] = frame[TOTAL_REFERENCE_COUNT_METRIC] - frame[TAIL_COUNT_METRIC]

    listed_per_timestep = top_classes.groupby("timestamp").size()
    excluded_class = top_classes.loc[top_classes["domain"].isin(excluded), ["timestamp", "domain", "class"]]
    excluded_info = (
        excluded_mass.merge(excluded_class, on=["timestamp", "domain"], how="left")
        .assign(listed_count=lambda df: df["timestamp"].map(listed_per_timestep))
    )
    # A domain with mass but without a logged class sits outside the top list, so its class is
    # unknown - unless the list is shorter than its limit, in which case every domain is on it.
    unknown = excluded_info["class"].isna() & (excluded_info["mass"] != 0) & (excluded_info["listed_count"] >= top_size)
    if unknown.any():
        log.warning(
            "Excluded domains outside the top %d at %d (timestep, domain) pairs; those timesteps are NaN.",
            top_size,
            int(unknown.sum()),
        )
    unknown_timestamps = set(excluded_info.loc[unknown, "timestamp"])

    removed_mass = excluded_info.groupby("timestamp")["mass"].sum()
    removed_by_class = excluded_info.dropna(subset=["class"]).pivot_table(
        index="timestamp", columns="class", values="mass", aggfunc="sum"
    )

    frame["excluded_mass"] = frame["timestamp"].map(removed_mass).fillna(0.0)
    frame["excluded_mass_fraction"] = frame["excluded_mass"] / frame["denominator"]
    remaining = frame["denominator"] - frame["excluded_mass"]
    for class_name in CLASSES:
        original = frame[f"{classifier}_{class_name}"]
        removed = (
            frame["timestamp"].map(removed_by_class[class_name]).fillna(0.0)
            if class_name in removed_by_class
            else 0.0
        )
        frame[class_name] = original
        frame[f"{class_name}_without"] = (original * frame["denominator"] - removed) / remaining

    frame.loc[frame["timestamp"].isin(unknown_timestamps), [f"{c}_without" for c in CLASSES]] = np.nan

    for domain in excluded:
        rows = excluded_info[excluded_info["domain"] == domain].set_index("timestamp")
        frame[f"mass[{domain}]"] = frame["timestamp"].map(rows["mass"]).fillna(0.0)
        frame[f"class[{domain}]"] = frame["timestamp"].map(rows["class"])

    columns = (
        ["timestamp", "denominator", "excluded_mass", "excluded_mass_fraction"]
        + [column for class_name in CLASSES for column in (class_name, f"{class_name}_without")]
        + [column for domain in excluded for column in (f"mass[{domain}]", f"class[{domain}]")]
    )
    return frame[columns]


def top_domain_counts(top_classes: pd.DataFrame, global_series: pd.DataFrame, classifier: str) -> pd.DataFrame:
    counts = top_classes.pivot_table(index="timestamp", columns="class", values="domain", aggfunc="count")
    masses = top_classes.pivot_table(index="timestamp", columns="class", values="mass", aggfunc="sum")
    counts = counts.reindex(columns=CLASSES).fillna(0).astype(int)
    masses = masses.reindex(columns=CLASSES).fillna(0.0)

    frame = pd.DataFrame({"timestamp": counts.index})
    frame["listed_domains"] = counts.sum(axis=1).to_numpy()
    for class_name in CLASSES:
        frame[f"{class_name}_count"] = counts[class_name].to_numpy()
    for class_name in CLASSES:
        frame[f"{class_name}_top_mass"] = masses[class_name].to_numpy()

    frame = attach_globals(frame, global_series)
    denominator = frame[TOTAL_REFERENCE_COUNT_METRIC] - frame[TAIL_COUNT_METRIC]
    for class_name in CLASSES:
        # share of the class's whole mass that sits in the listed domains
        class_mass = frame[f"{classifier}_{class_name}"] * denominator
        frame[f"{class_name}_top_coverage"] = frame[f"{class_name}_top_mass"] / class_mass.where(class_mass > 0)

    columns = (
        ["timestamp", "listed_domains"]
        + [f"{c}_count" for c in CLASSES]
        + [f"{c}_top_mass" for c in CLASSES]
        + [f"{c}_top_coverage" for c in CLASSES]
    )
    return frame[columns]


def excluded_file_tag(excluded: list[str]) -> str:
    """File-name-safe tag naming the excluded domains, so each exclusion set gets its own CSV."""
    return "+".join(re.sub(r"[^\w.-]+", "_", domain) for domain in excluded)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    parser.add_argument(
        "--classifier",
        help="Metric prefix, e.g. flow_classification_supermajority. Detected from the run if omitted.",
    )
    parser.add_argument(
        "--exclude",
        action="extend",
        nargs="+",
        default=[],
        metavar="DOMAIN",
        help="Domains to leave out together (--exclude a b, or repeat the flag). "
        "Defaults to the largest domain at the last timestep.",
    )
    parser.add_argument(
        "--top-size",
        type=int,
        default=1000,
        help="top_largest_domains_to_write the run used; only needed to tell a truncated list from a complete one.",
    )
    parser.add_argument("--out-dir", default=".", help="Directory the two CSV files are written to.")
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(message)s",
    )

    engine = build_engine_read_only()
    classifier = args.classifier or detect_classifier(engine, args.run_id)
    log.info("Run %d, classifier %s", args.run_id, classifier)

    ids = metric_ids(engine, [MASS_METRIC, classifier + CLASSIFICATION_SUFFIX])
    global_names = [TOTAL_REFERENCE_COUNT_METRIC, TAIL_COUNT_METRIC] + [f"{classifier}_{c}" for c in CLASSES]
    global_series = load_global_series(engine, args.run_id, global_names)

    top_classes = load_top_domain_classes(
        engine, args.run_id, ids[classifier + CLASSIFICATION_SUFFIX], ids[MASS_METRIC]
    )
    if top_classes.empty:
        raise RuntimeError(f"Run {args.run_id} has no per-domain classification.")
    log.info(
        "Loaded %d per-domain classes over %d timesteps", len(top_classes), top_classes["timestamp"].nunique()
    )

    excluded = list(dict.fromkeys(args.exclude))
    if not excluded:
        last = top_classes[top_classes["timestamp"] == top_classes["timestamp"].max()]
        excluded = [last.loc[last["mass"].idxmax(), "domain"]]
        log.info("No --exclude given; excluding the largest domain at the last timestep: %s", excluded[0])

    excluded_mass = load_domain_mass(
        engine, args.run_id, ids[MASS_METRIC], pd.Series(top_classes["timestamp"].unique()), excluded
    )

    without = fractions_without(top_classes, excluded_mass, global_series, classifier, excluded, args.top_size)
    counts = top_domain_counts(top_classes, global_series, classifier)

    os.makedirs(args.out_dir, exist_ok=True)
    without_path = os.path.join(
        args.out_dir, f"class_fractions_without_{excluded_file_tag(excluded)}_run{args.run_id}.csv"
    )
    counts_path = os.path.join(args.out_dir, f"class_domain_counts_top{args.top_size}_run{args.run_id}.csv")
    without.to_csv(without_path, index=False)
    counts.to_csv(counts_path, index=False)

    last_without = without.iloc[-1]
    last_counts = counts.iloc[-1]
    print(f"\nLast timestep {last_without['timestamp']}, excluding {', '.join(excluded)} "
          f"({last_without['excluded_mass_fraction']:.2%} of the mass)")
    print(f"{'class':<14}{'fraction':>10}{'without':>10}{'top count':>11}{'top coverage':>14}")
    for class_name in CLASSES:
        print(
            f"{class_name:<14}{last_without[class_name]:>10.2%}{last_without[f'{class_name}_without']:>10.2%}"
            f"{last_counts[f'{class_name}_count']:>11d}{last_counts[f'{class_name}_top_coverage']:>14.2%}"
        )
    print(f"\nWrote {without_path}\nWrote {counts_path}")


if __name__ == "__main__":
    main()
