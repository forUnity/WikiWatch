"""Total mass and share of a group of reference domains at a run's last timestep.

By default the group is Wikipedia: every Wikipedia language edition on Wikidata
(instances of Q10876391 "Wikipedia language edition" or a subclass of it, which
adds the former editions) plus Q52 "Wikipedia" itself, as used by references
that are not URLs (mostly P143 "imported from Wikimedia project"). The list is
kept in ``wikipedia_editions.csv`` next to this script and rebuilt from the
Wikidata Query Service with ``--refresh-wikipedia``. URL references are grouped
by host, so references to Wikipedia pages show up as ``en.wikipedia.org`` and
alike; those are matched separately by host suffix and reported alongside.

Shares are given against two totals of the size metric at that timestep:
  * all references, including the discarded tail of small domains
    (``reference_counts_per_domain_total_reference_count``), and
  * the per-domain mass, i.e. all but the tail -- what the classifiers' class
    fractions (figure 6) are fractions of.

A listed domain without a row at the last timestep was either never used or has
been discarded into the tail; its references then only count towards the tail.

Query cost: the last timestep is a MAX over the primary key (an index lookup),
the listed domains are full-PK probes, and the host-suffix match reads the size
metric at that single timestep only.

Usage:
    python domain_group_share.py                          # Wikipedia, default run
    python domain_group_share.py --domains Q328 europepmc.org Q5412157
    python domain_group_share.py --domains-file my_domains.txt --host-suffix ncbi.nlm.nih.gov
    python domain_group_share.py --refresh-wikipedia      # re-query the edition list first
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path

import pandas as pd

from domain_classification_details import (
    DEFAULT_RUN_ID,
    DEFAULT_SIZE_METRIC,
    _TAIL_MASS_STAT,
    _TOTAL_MASS_STAT,
    _connect,
    _empty_stats,
    _format_mass,
    _format_share,
    _to_db_timestamp,
    build_yearly_mass,
    fetch_cells,
    fetch_stats,
    resolve_labels,
    resolve_metric_ids,
)

WIKIPEDIA_LIST = Path(__file__).resolve().parent / "wikipedia_editions.csv"
WIKIPEDIA_HOST_SUFFIX = "wikipedia.org"

SPARQL_ENDPOINT = "https://query.wikidata.org/sparql"
USER_AGENT = "mp2025-wikiwatch-domain-group-share/1.0 (hpi.de)"
# Every Wikipedia language edition, current or former (a subclass), plus the
# project item itself, which some references name instead of an edition.
WIKIPEDIA_QUERY = """
SELECT DISTINCT ?item ?itemLabel WHERE {
  { ?item wdt:P31/wdt:P279* wd:Q10876391 . }
  UNION
  { VALUES ?item { wd:Q52 } }
  SERVICE wikibase:label { bd:serviceParam wikibase:language "en". }
}
"""


# --------------------------------------------------------------------------
# The domain list
# --------------------------------------------------------------------------


def refresh_wikipedia_list(path: Path = WIKIPEDIA_LIST, timeout: int = 60) -> None:
    """Query the Wikidata Query Service for every Wikipedia and write ``path``."""
    import requests

    response = requests.get(
        SPARQL_ENDPOINT,
        params={"query": WIKIPEDIA_QUERY, "format": "json"},
        headers={"User-Agent": USER_AGENT, "Accept": "application/sparql-results+json"},
        timeout=timeout,
    )
    response.raise_for_status()
    rows = {}
    for binding in response.json()["results"]["bindings"]:
        qid = binding["item"]["value"].rsplit("/", 1)[-1]
        rows[qid] = binding.get("itemLabel", {}).get("value", "")
    if not rows:
        raise SystemExit("The Wikidata Query Service returned no Wikipedia editions.")

    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["qid", "label"])
        for qid in sorted(rows, key=lambda value: int(value[1:])):
            writer.writerow([qid, rows[qid]])
    print(f"Wrote {len(rows)} Wikipedia editions to {path}")


def load_wikipedia_list(path: Path = WIKIPEDIA_LIST) -> dict[str, str]:
    """QID -> label of every Wikipedia, from the list file."""
    frame = pd.read_csv(path, dtype=str, keep_default_na=False)
    return dict(zip(frame["qid"], frame["label"]))


def read_domains_file(path: Path) -> list[str]:
    """One domain per line; blank lines and ``#`` comments are ignored."""
    domains = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            domains.append(line)
    return domains


# --------------------------------------------------------------------------
# Database access
# --------------------------------------------------------------------------


def fetch_last_timestep(conn, run_id: int, size_metric_id: int) -> pd.Timestamp | None:
    """The run's last timestep of the size metric: a backward walk of the PK."""
    frame = pd.read_sql_query(
        """
        SELECT MAX(timestamp) AS timestamp
        FROM metric_on_string
        WHERE run_id = %s AND metric_id = %s
        """,
        conn,
        params=(run_id, size_metric_id),
    )
    value = frame["timestamp"].iloc[0]
    if pd.isna(value):
        return None
    # The column is timestamp without time zone, written in UTC.
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        return timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def fetch_hosts_with_suffix(
    conn, run_id: int, size_metric_id: int, timestamp: pd.Timestamp, suffix: str
) -> pd.DataFrame:
    """Every domain equal to ``suffix`` or ending in ``.suffix``, with its mass.

    Pins (run_id, metric_id, timestamp), so it reads one timestep of the size
    metric -- a few hundred thousand index entries -- and never the whole run.
    """
    escaped = suffix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return pd.read_sql_query(
        """
        SELECT key_string AS domain, value AS mass
        FROM metric_on_string
        WHERE run_id = %s
          AND metric_id = %s
          AND timestamp = %s
          AND (key_string = %s OR key_string LIKE %s)
        """,
        conn,
        params=(run_id, size_metric_id, _to_db_timestamp(timestamp), suffix, f"%.{escaped}"),
    )


# --------------------------------------------------------------------------
# Report
# --------------------------------------------------------------------------


def summarise_group(
    name: str,
    masses: pd.DataFrame,
    all_references: float,
    per_domain_mass: float,
) -> dict:
    """One total line: mass of the group and its share of both totals."""
    mass = float(masses["mass"].sum())
    return {
        "group": name,
        "domains_present": int(len(masses)),
        "mass": mass,
        "share_of_all_references": mass / all_references if all_references > 0 else float("nan"),
        "share_of_per_domain_mass": mass / per_domain_mass if per_domain_mass > 0 else float("nan"),
    }


_ROW = "  {:>4}  {:<38} {:>15}  {:>7}  {:>7}  {}"


def print_domains(title: str, frame: pd.DataFrame, show: int) -> None:
    print()
    print(title)
    if frame.empty:
        print("  (none present at this timestep)")
        return
    print(_ROW.format("#", "domain", "references", "% all", "% dom.", "label"))
    for rank, row in enumerate(frame.head(show).itertuples(index=False), start=1):
        print(
            _ROW.format(
                rank,
                str(row.domain)[:38],
                _format_mass(row.mass),
                _format_share(row.share_of_all_references),
                _format_share(row.share_of_per_domain_mass),
                row.label,
            ).rstrip()
        )
    if len(frame) > show:
        print(f"  ... {len(frame) - show} more in the CSV")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Total mass and share of a group of reference domains at the run's "
            "last timestep (default group: every Wikipedia)."
        )
    )
    parser.add_argument("--run-id", type=int, default=DEFAULT_RUN_ID)
    parser.add_argument("--size-metric", default=DEFAULT_SIZE_METRIC)
    parser.add_argument(
        "--domains", nargs="+", default=None, metavar="DOMAIN",
        help="domains to sum (default: every Wikipedia edition QID)",
    )
    parser.add_argument(
        "--domains-file", type=Path, default=None,
        help="file with one domain per line, added to --domains",
    )
    parser.add_argument(
        "--host-suffix", nargs="+", default=None, metavar="SUFFIX",
        help=(
            "also sum every domain equal to or ending in .SUFFIX, as its own group "
            f"(default: {WIKIPEDIA_HOST_SUFFIX} when no domains are given, else none)"
        ),
    )
    parser.add_argument(
        "--refresh-wikipedia", action="store_true",
        help=f"re-query the Wikipedia edition list from Wikidata into {WIKIPEDIA_LIST.name}",
    )
    parser.add_argument(
        "--show", type=int, default=25,
        help="domains listed per group on the console (default: 25; all are in the CSV)",
    )
    parser.add_argument("--outdir", type=Path, default=Path("."))
    parser.add_argument(
        "--offline", action="store_true",
        help="no Wikidata label lookups for domains outside the Wikipedia list",
    )
    parser.add_argument(
        "--timeout", type=int, default=300,
        help="statement_timeout in seconds (default: 300)",
    )
    args = parser.parse_args(argv)

    if args.refresh_wikipedia and args.offline:
        parser.error("--refresh-wikipedia needs network access; drop --offline")
    explicit = list(args.domains or [])
    if args.domains_file is not None:
        explicit += read_domains_file(args.domains_file)
    args.explicit_domains = list(dict.fromkeys(explicit))
    args.default_group = not args.explicit_domains
    if args.host_suffix is None:
        args.host_suffix = [WIKIPEDIA_HOST_SUFFIX] if args.default_group else []
    return args


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)

    known_labels: dict[str, str] = {}
    if args.default_group:
        if args.refresh_wikipedia or not WIKIPEDIA_LIST.exists():
            refresh_wikipedia_list()
        known_labels = load_wikipedia_list()
        listed = list(known_labels)
        listed_name = "Wikipedia editions (QIDs)"
    else:
        listed = args.explicit_domains
        listed_name = "listed domains"

    conn = _connect(args.timeout)
    try:
        ids = resolve_metric_ids(conn, [args.size_metric])
        if args.size_metric not in ids:
            raise SystemExit(f"Could not resolve the size metric {args.size_metric!r}.")
        size_id = ids[args.size_metric]

        last = fetch_last_timestep(conn, args.run_id, size_id)
        if last is None:
            raise SystemExit(f"Run {args.run_id} has no rows for {args.size_metric!r}.")

        listed_masses = fetch_cells(conn, args.run_id, size_id, [last], listed)
        listed_masses = listed_masses.rename(
            columns={"key_string": "domain", "value": "mass"}
        )[["domain", "mass"]]
        host_masses = {
            suffix: fetch_hosts_with_suffix(conn, args.run_id, size_id, last, suffix)
            for suffix in args.host_suffix
        }
        size_stats = fetch_stats(conn, args.run_id, args.size_metric)
    finally:
        conn.close()

    totals = build_yearly_mass(_empty_stats(), size_stats, [last]).iloc[0]
    all_references = float(totals[_TOTAL_MASS_STAT])
    tail = float(totals[_TAIL_MASS_STAT])
    per_domain_mass = float(totals["total_mass"])

    groups = {listed_name: listed_masses}
    for suffix, frame in host_masses.items():
        # A listed domain that also matches a suffix is counted in its list only.
        groups[f"hosts *.{suffix}"] = frame[~frame["domain"].isin(listed)]

    present = pd.concat(groups.values(), ignore_index=True)["domain"].unique()
    labels = dict(known_labels)
    missing = [domain for domain in present if domain not in labels]
    if missing:
        labels.update(resolve_labels(missing, args.offline))

    print(f"Run {args.run_id}, last timestep {last:%Y-%m-%d %H:%M} UTC")
    print(
        f"All references: {_format_mass(all_references)} "
        f"(of which {_format_mass(tail)} in the discarded tail; "
        f"per-domain mass {_format_mass(per_domain_mass)})"
    )

    detail_frames, summary_rows = [], []
    for name, frame in groups.items():
        frame = frame.sort_values("mass", ascending=False, ignore_index=True)
        frame["label"] = frame["domain"].map(labels).fillna("")
        frame["share_of_all_references"] = frame["mass"] / all_references
        frame["share_of_per_domain_mass"] = frame["mass"] / per_domain_mass
        frame.insert(0, "group", name)
        detail_frames.append(frame)
        summary_rows.append(summarise_group(name, frame, all_references, per_domain_mass))
        print_domains(f"{name}: {len(frame)} present", frame, args.show)

    absent = len(listed) - len(listed_masses)
    if absent:
        print(
            f"\n{absent} of the {len(listed)} {listed_name} have no row at this "
            "timestep: never used, or discarded into the tail (then only counted there)."
        )

    summary = pd.DataFrame(summary_rows)
    if len(groups) > 1:
        summary = pd.concat(
            [
                summary,
                pd.DataFrame(
                    [
                        summarise_group(
                            "combined",
                            pd.concat(detail_frames, ignore_index=True),
                            all_references,
                            per_domain_mass,
                        )
                    ]
                ),
            ],
            ignore_index=True,
        )

    print()
    print("Totals")
    row_format = "  {:<34} {:>8} {:>17}  {:>13}  {:>15}"
    print(row_format.format("group", "domains", "references", "% of all refs", "% of dom. mass"))
    for row in summary.itertuples(index=False):
        print(
            row_format.format(
                row.group[:34],
                row.domains_present,
                _format_mass(row.mass),
                _format_share(row.share_of_all_references),
                _format_share(row.share_of_per_domain_mass),
            )
        )

    output_dir = args.outdir / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = "wikipedia_share" if args.default_group else "domain_group_share"
    detail_path = output_dir / f"{stem}_{args.run_id}_{last:%Y-%m-%d}.csv"
    summary_path = output_dir / f"{stem}_totals_{args.run_id}_{last:%Y-%m-%d}.csv"
    pd.concat(detail_frames, ignore_index=True).to_csv(detail_path, index=False)
    summary.to_csv(summary_path, index=False)
    print()
    print(f"Wrote {detail_path}")
    print(f"Wrote {summary_path}")


if __name__ == "__main__":
    main()
