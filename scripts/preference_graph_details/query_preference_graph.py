"""Interactive neighbour explorer for the reference preference graphs.

The trust-graph statistics write one cumulative directed graph per snapshot date
into ``preference_graphs/``, as a graph-tool binary (``.gt.gz``) plus a portable
gzipped edge list (``.csv.gz``) with the columns::

    replaced_domain, preferred_domain, weight

An edge ``x -> y`` means **x was replaced in favour of y**: within a concluded
editing session the reference on domain ``x`` disappeared while the one on ``y``
survived.  ``weight`` counts how often that preference was expressed, cumulative
up to the snapshot date.  Both directions can exist between the same pair, which
is what makes a pairing contested rather than one-sided.

This tool loads one snapshot once and then answers domain queries interactively.
For a domain ``x`` it prints two tables, each ordered by weight descending:

  * the domains ``x`` points *to* -- edges ``x -> y``, i.e. the domains that were
    preferred over ``x``, the ones ``x`` was replaced by;
  * the domains that point *to* ``x`` -- edges ``y -> x``, i.e. the domains ``x``
    was preferred over, the ones ``x`` replaced.

Each row also carries the weight of the reverse edge and the net margin, because
the reverse of an ``x -> y`` edge is by definition a ``y -> x`` edge and so is
already in the other table.

When the domain mass of a run has been exported next to the snapshot (by
``scripts/export_preference_graph_domain_mass.py``, as
``preference_graph_<date>.domain_mass_run<run_id>.tsv.gz``), every domain is shown
with its mass -- its number of references at the snapshot date -- and every
weight also as a share of the mass of the domain being examined.

Roughly half the nodes are bare Wikidata QIDs rather than hostnames: the
reference-domain extractor passes through anything that is not a URL, and item
references (``Q328`` = English Wikipedia) fall into that bucket.  Displayed QIDs
are therefore resolved to labels through the Wikidata API, batched, cached on
disk and fully guarded -- an offline machine degrades to bare QIDs rather than
failing.

The ``.gt.gz`` files are not read: graph-tool is not pip-installable and is
absent from most environments here, while the edge list needs only pandas.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

try:  # Optional: without it the tool still works, just without labels.
    import requests
except ImportError:  # pragma: no cover - depends on the environment
    requests = None

LOGGER = logging.getLogger(__name__)

DEFAULT_TOP = 50
DEFAULT_LANG = "en"
DEFAULT_TIMEOUT = 10
DEFAULT_LABEL_CACHE = Path(__file__).resolve().parent / "qid_labels.json"

EDGE_COLUMNS = ("replaced_domain", "preferred_domain", "weight")
SNAPSHOT_SUFFIX = ".csv.gz"
SNAPSHOT_GLOB = "*" + SNAPSHOT_SUFFIX
PARTIAL_MARKER = ".partial."
YEAR_PATTERN = re.compile(r"\d{4}")
MASS_RUN_PATTERN = re.compile(r"\.domain_mass_run(\d+)\.tsv\.gz$")

QID_PATTERN = re.compile(r"Q\d+")
WIKIDATA_API = "https://www.wikidata.org/w/api.php"
USER_AGENT = "mp2025-wikiwatch-preference-graph/1.0 (hpi.de)"
LABEL_BATCH_SIZE = 40
LABEL_BATCH_PAUSE = 0.1
THROTTLED_PAUSE = 2.0


# --------------------------------------------------------------------------- #
# Snapshot files
# --------------------------------------------------------------------------- #


def complete_snapshots(directory: Path) -> list[Path]:
    """The complete edge lists in `directory`, oldest first.

    Snapshot names end in the timeslice's end date (``preference_graph_%Y-%m-%d``),
    so lexicographic order is chronological.  Files still being written carry
    ``.partial.`` before the extension and are skipped.
    """
    return [
        path
        for path in sorted(directory.glob(SNAPSHOT_GLOB))
        if PARTIAL_MARKER not in path.name
    ]


def latest_snapshot(directory: Path, year: str | None = None) -> Path | None:
    """The newest complete edge list in `directory` (from `year`, if given), or None."""
    candidates = [
        path
        for path in complete_snapshots(directory)
        if year is None or f"_{year}-" in path.name
    ]
    return candidates[-1] if candidates else None


def resolve_snapshot(path: Path) -> Path:
    """Turn a user-supplied path into the edge list to read.

    Accepts the edge list itself, a directory of snapshots (newest is taken), or
    a ``.gt.gz`` graph (its sibling edge list is taken, since graph-tool is not
    generally installed).  Raises FileNotFoundError with a usable message.
    """
    path = path.expanduser()

    if path.is_dir():
        snapshot = latest_snapshot(path)
        if snapshot is None:
            raise FileNotFoundError(f"no {SNAPSHOT_GLOB} snapshot in {path}")
        return snapshot

    if path.name.endswith(".gt.gz"):
        sibling = path.with_name(path.name[: -len(".gt.gz")] + ".csv.gz")
        if not sibling.exists():
            raise FileNotFoundError(
                f"{path} is a graph-tool file, which this tool does not read, "
                f"and its edge list {sibling.name} is missing"
            )
        LOGGER.info("Reading the edge list %s instead of the graph.", sibling.name)
        return sibling

    if not path.exists():
        raise FileNotFoundError(f"no such file or directory: {path}")
    return path


# --------------------------------------------------------------------------- #
# Domain mass
# --------------------------------------------------------------------------- #


def mass_files(snapshot: Path) -> dict[int, Path]:
    """run_id -> domain mass table exported for `snapshot`.

    Written by scripts/export_preference_graph_domain_mass.py as
    ``preference_graph_<date>.domain_mass_run<run_id>.tsv.gz`` next to the graph.
    """
    stem = snapshot.name.removesuffix(SNAPSHOT_SUFFIX)
    found = {}
    for path in snapshot.parent.glob(f"{stem}.domain_mass_run*.tsv.gz"):
        match = MASS_RUN_PATTERN.search(path.name)
        if match:
            found[int(match.group(1))] = path
    return found


class DomainMass:
    """The reference mass per domain of one run at the snapshot's timestamp.

    Domains missing from the table were discarded into the long tail by the
    domain count metric, so their mass is small but unknown.
    """

    def __init__(self, path: Path, run_id: int) -> None:
        self.path = path
        self.run_id = run_id
        table = pd.read_csv(
            path, sep="\t", compression="infer", keep_default_na=False, dtype={"domain": str}
        )
        self.count: dict[str, float] = dict(zip(table["domain"], table["count"].astype(float)))

        # The total reference count (tail included) lives in the vertex-aligned
        # .npz sibling; fraction = count / total recovers it when that is missing.
        self.total = float("nan")
        npz_path = path.with_name(path.name.removesuffix(".tsv.gz") + ".npz")
        try:
            with np.load(npz_path) as data:
                self.total = float(data["total_reference_count"])
        except Exception as error:
            LOGGER.debug("No total reference count in %s (%s).", npz_path, error)
        if np.isnan(self.total):
            usable = table[table["fraction"] > 0]
            if not usable.empty:
                self.total = float((usable["count"] / usable["fraction"]).median())

        LOGGER.info(
            "Loaded the domain mass of run %s (%s domains) from %s.",
            run_id,
            f"{len(self.count):,}",
            path.name,
        )

    def get(self, domain: str) -> float | None:
        return self.count.get(domain)


def load_mass(snapshot: Path, run_id: int | None) -> DomainMass | None:
    """The mass table for `snapshot`, or None (with a message) when there is none to use.

    Without `run_id` the latest exported run (highest id) is taken.
    """
    available = mass_files(snapshot)
    if run_id is not None:
        if run_id not in available:
            LOGGER.warning(
                "No domain mass of run %s for %s (available runs: %s); showing no mass.",
                run_id,
                snapshot.name,
                sorted(available) or "none",
            )
            return None
        return DomainMass(available[run_id], run_id)

    if not available:
        LOGGER.warning(
            "No domain mass file for %s; showing no mass. Run scripts/export_preference_graph_domain_mass.py to add it.",
            snapshot.name,
        )
        return None
    # Run ids are SLURM job ids, so the highest is the latest run.
    latest_run = max(available)
    if len(available) > 1:
        LOGGER.warning(
            "Domain mass of several runs exported for %s (%s); using the latest, run %s. "
            "Pick another with --run-id or :run.",
            snapshot.name,
            sorted(available),
            latest_run,
        )
    return DomainMass(available[latest_run], latest_run)


def shorthand(value: float | None) -> str:
    """1234 -> 1.2k, 5_600_000 -> 5.6M; None (discarded tail) -> 'tail'."""
    if value is None:
        return "tail"
    if np.isnan(value):
        return "?"
    magnitude = abs(value)
    for threshold, suffix in ((1e9, "B"), (1e6, "M"), (1e3, "k")):
        if magnitude >= threshold:
            scaled = value / threshold
            return f"{scaled:.0f}{suffix}" if abs(scaled) >= 100 else f"{scaled:.1f}{suffix}"
    return f"{value:.0f}"


def percent(numerator: float, denominator: float | None) -> str:
    """`numerator` as a readable share of `denominator`, or '-' if that is undefined."""
    if denominator is None or np.isnan(denominator) or denominator <= 0:
        return "-"
    share = 100.0 * numerator / denominator
    if share == 0:
        return "0%"
    if share >= 10:
        return f"{share:.0f}%"
    if share >= 0.1:
        return f"{share:.1f}%"
    return f"{share:.2g}%"


# --------------------------------------------------------------------------- #
# The graph
# --------------------------------------------------------------------------- #


class PreferenceGraph:
    """One snapshot, held as parallel arrays of integer node codes.

    The frame is factorised once so a query is a vectorised scan over ~2.4M
    int-coded endpoints (milliseconds) rather than a comparison of 2.4M Python
    strings.  Node names are kept in `self.names`, indexed by code.
    """

    def __init__(self, path: Path, run_id: int | None = None) -> None:
        self.path = path
        started = time.perf_counter()
        edges = pd.read_csv(path, compression="infer")

        missing = [column for column in EDGE_COLUMNS if column not in edges.columns]
        if missing:
            raise ValueError(
                f"{path} is missing the column(s) {', '.join(missing)}; expected a "
                f"preference-graph edge list with {', '.join(EDGE_COLUMNS)}"
            )

        endpoint_codes, self.names = pd.factorize(
            pd.concat(
                [edges["replaced_domain"], edges["preferred_domain"]],
                ignore_index=True,
            )
        )
        edge_count = len(edges)
        self.src = endpoint_codes[:edge_count]
        self.dst = endpoint_codes[edge_count:]
        self.weight = edges["weight"].to_numpy()
        self.code_of = {name: code for code, name in enumerate(self.names)}

        LOGGER.info(
            "Loaded %s: %s edges over %s domains in %.2fs.",
            path.name,
            f"{edge_count:,}",
            f"{len(self.names):,}",
            time.perf_counter() - started,
        )

        self.mass: DomainMass | None = load_mass(path, run_id)

    @property
    def edge_count(self) -> int:
        return len(self.weight)

    def neighbours(self, domain: str) -> tuple[dict[str, int], dict[str, int]] | None:
        """(preferred over `domain`, `domain` preferred over), or None if unknown.

        The first dict holds the targets of the outgoing edges ``domain -> y``,
        the second the sources of the incoming edges ``y -> domain``, each mapped
        to that edge's weight.  Returned whole rather than truncated, so callers
        can report totals and look up reverse weights in the opposite dict.
        """
        code = self.code_of.get(domain)
        if code is None:
            return None

        outgoing = np.flatnonzero(self.src == code)
        incoming = np.flatnonzero(self.dst == code)
        return (
            {self.names[self.dst[i]]: int(self.weight[i]) for i in outgoing},
            {self.names[self.src[i]]: int(self.weight[i]) for i in incoming},
        )


def rank(edges: dict[str, int], reverse: dict[str, int], top: int) -> list[tuple]:
    """The `top` heaviest entries of `edges` as (domain, weight, reverse, net).

    Sorted by weight descending, ties broken by domain name so repeated runs
    print the same order.
    """
    ordered = sorted(edges.items(), key=lambda item: (-item[1], item[0]))[:top]
    return [
        (domain, weight, reverse.get(domain, 0), weight - reverse.get(domain, 0))
        for domain, weight in ordered
    ]


# --------------------------------------------------------------------------- #
# Wikidata labels
# --------------------------------------------------------------------------- #


class LabelResolver:
    """QID -> label, backed by a JSON file and the Wikidata API.

    Every call is guarded: a missing `requests`, a timeout, a throttling response
    or an unparseable payload leaves the QID unresolved and logs a warning, so
    the tool stays usable offline and on the cluster.  A QID the API returned
    without a label in the requested language is cached as an empty string and
    not asked for again; a failed request caches nothing, so the next run
    retries it.
    """

    def __init__(
        self,
        cache_path: Path,
        *,
        fetch: bool = True,
        lang: str = DEFAULT_LANG,
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self.cache_path = cache_path
        self.fetch = fetch
        self.lang = lang
        self.timeout = timeout
        self.labels: dict[str, str] = self._load_cache()

    def _load_cache(self) -> dict[str, str]:
        if not self.cache_path.exists():
            return {}
        try:
            with self.cache_path.open(encoding="utf-8") as handle:
                cached = json.load(handle)
            labels = {
                qid: label
                for qid, label in cached.items()
                if QID_PATTERN.fullmatch(str(qid)) and isinstance(label, str)
            }
            LOGGER.debug("Read %s cached labels from %s.", len(labels), self.cache_path)
            return labels
        except Exception as error:
            LOGGER.warning("Ignoring unreadable label cache %s (%s).", self.cache_path, error)
            return {}

    def _save_cache(self) -> None:
        """Write through a sibling temp file, so a kill leaves the old cache intact."""
        temp_path = self.cache_path.with_suffix(self.cache_path.suffix + ".partial")
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(self.labels, handle, ensure_ascii=False, indent=1, sort_keys=True)
            os.replace(temp_path, self.cache_path)
        except Exception as error:
            LOGGER.warning("Could not write the label cache %s (%s).", self.cache_path, error)

    def resolve(self, names: list[str]) -> None:
        """Make sure every QID among `names` is in `self.labels`, if it can be.

        Hostnames and the ``ISBN``/``DOI``/``PMID``/``__INVALID_URL__`` buckets
        never reach the network.
        """
        wanted = {
            name for name in names if QID_PATTERN.fullmatch(str(name))
        } - self.labels.keys()
        if not wanted:
            return
        if not self.fetch:
            LOGGER.debug("Not resolving %s QID(s): label fetching is off.", len(wanted))
            return
        if requests is None:
            LOGGER.warning("Cannot resolve QIDs: the requests package is not installed.")
            return

        fetched = self._fetch(sorted(wanted))
        if fetched:
            self.labels.update(fetched)
            self._save_cache()

    def _fetch(self, qids: list[str]) -> dict[str, str]:
        labels: dict[str, str] = {}
        for start in range(0, len(qids), LABEL_BATCH_SIZE):
            batch = qids[start : start + LABEL_BATCH_SIZE]
            # The API answers a batch holding one nonexistent (e.g. deleted) QID
            # with an error for the whole batch, naming that QID. Drop it, cache
            # it as having no label, and ask again for the rest.
            while batch:
                try:
                    response = requests.get(
                        WIKIDATA_API,
                        params={
                            "action": "wbgetentities",
                            "props": "labels",
                            "ids": "|".join(batch),
                            "languages": self.lang,
                            "format": "json",
                        },
                        timeout=self.timeout,
                        headers={"User-Agent": USER_AGENT},
                    )
                    if response.status_code == 403:
                        LOGGER.warning("Wikidata refused a label batch (403); backing off.")
                        time.sleep(THROTTLED_PAUSE)
                        break
                    response.raise_for_status()
                    payload = response.json()
                except Exception as error:
                    # Keep the tool usable on offline clusters: skip, never raise.
                    LOGGER.warning(
                        "Could not resolve labels for the batch starting at %s (%s).",
                        batch[0],
                        error,
                    )
                    break
                time.sleep(LABEL_BATCH_PAUSE)

                error = payload.get("error")
                if error:
                    missing = error.get("id")
                    if error.get("code") == "no-such-entity" and missing in batch:
                        LOGGER.debug("%s does not exist on Wikidata.", missing)
                        labels[missing] = ""
                        batch = [qid for qid in batch if qid != missing]
                        continue
                    LOGGER.warning(
                        "Wikidata rejected the label batch starting at %s (%s).",
                        batch[0],
                        error.get("info", error.get("code")),
                    )
                    break

                for qid, entity in payload.get("entities", {}).items():
                    label = entity.get("labels", {}).get(self.lang, {}).get("value", "")
                    labels[qid] = label
                break

        LOGGER.debug("Resolved %s of %s requested QID(s).", len(labels), len(qids))
        return labels

    def describe(self, name: str) -> str:
        """`name` with its label appended, when it is a QID with a known one."""
        label = self.labels.get(name)
        return f"{name} ({label})" if label else name


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


def print_table(
    title: str,
    subtitle: str,
    rows: list[tuple],
    resolver: LabelResolver,
    mass: DomainMass | None,
    focus_mass: float | None,
) -> None:
    """One neighbour table; with `mass`, each domain's mass and the weight as a
    share of the examined domain's mass (`focus_mass`) are added."""
    print()
    print(f"{title}   {subtitle}")
    if not rows:
        print("  (none)")
        return

    headers = ["rank", "domain"]
    table = [[str(position), resolver.describe(domain)] for position, (domain, *_) in enumerate(rows, start=1)]
    if mass is not None:
        headers.append("mass")
        for cells, (domain, *_) in zip(table, rows):
            cells.append(shorthand(mass.get(domain)))
    headers.append("weight")
    for cells, (_, weight, _, _) in zip(table, rows):
        cells.append(f"{weight:,}")
    if mass is not None:
        headers.append("wt/mass")
        for cells, (_, weight, _, _) in zip(table, rows):
            cells.append(percent(weight, focus_mass))
    headers += ["reverse", "net"]
    for cells, (_, _, reverse, net) in zip(table, rows):
        cells += [f"{reverse:,}", f"{net:,}"]

    widths = [max(len(header), max(len(cells[index]) for cells in table)) for index, header in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return "  " + "  ".join(
            cell.ljust(width) if header == "domain" else cell.rjust(width)
            for cell, width, header in zip(cells, widths, headers)
        )

    print(line(headers))
    for cells in table:
        print(line(cells))


def report(graph: PreferenceGraph, resolver: LabelResolver, domain: str, top: int) -> None:
    """Print both neighbour tables for `domain`."""
    mass = graph.mass
    focus_mass = mass.get(domain) if mass is not None else None

    found = graph.neighbours(domain)
    if found is None:
        print(f"\n{domain}: not in this snapshot ({graph.path.name}).")
        if focus_mass is not None:
            print(f"  mass {shorthand(focus_mass)} references (run {mass.run_id})")
        return
    outgoing, incoming = found

    out_rows = rank(outgoing, incoming, top)
    in_rows = rank(incoming, outgoing, top)
    resolver.resolve([domain] + [row[0] for row in out_rows] + [row[0] for row in in_rows])

    print()
    print("=" * 78)
    print(resolver.describe(domain))
    if mass is not None:
        if focus_mass is None:
            print(f"  mass        in the discarded tail (run {mass.run_id}), so no wt/mass")
        elif np.isnan(mass.total):
            print(f"  mass        {shorthand(focus_mass)} references (run {mass.run_id}; total unknown)")
        else:
            print(
                f"  mass        {shorthand(focus_mass)} references, "
                f"{percent(focus_mass, mass.total)} of {shorthand(mass.total)} (run {mass.run_id})"
            )
    for label, edges in (("replaced by", outgoing), ("replaced   ", incoming)):
        total_weight = sum(edges.values())
        share = f" ({percent(total_weight, focus_mass)} of its mass)" if focus_mass is not None else ""
        print(f"  {label} {len(edges):,} domain(s), total weight {total_weight:,}{share}")
    if mass is not None:
        print(f"  mass = the row domain's references; wt/mass = weight / mass of {domain}; tail = discarded tail")
    print("=" * 78)

    print_table(
        f"PREFERRED OVER {domain}",
        f"(edges {domain} -> y: y replaced {domain}) -- top {len(out_rows)}",
        out_rows,
        resolver,
        mass,
        focus_mass,
    )
    print_table(
        f"{domain} PREFERRED OVER",
        f"(edges y -> {domain}: {domain} replaced y) -- top {len(in_rows)}",
        in_rows,
        resolver,
        mass,
        focus_mass,
    )


# --------------------------------------------------------------------------- #
# Interactive loop
# --------------------------------------------------------------------------- #

HELP = """\
Type a domain name (or a QID) to see its neighbours in the loaded snapshot.
Commands:
  :top N              rows per direction for later queries
  :snapshots          list the snapshots next to the loaded one
  :load YEAR|PATH     load another snapshot (YEAR, e.g. 2025: the newest of that year)
  :run ID             use the domain mass exported for this run id
  :labels on|off      toggle Wikidata label lookups
  :help               this text
  :quit               leave (Ctrl-C and Ctrl-D also work)"""


def resolve_load_argument(current: Path, argument: str) -> Path:
    """A `:load` argument: a year (newest sibling snapshot of that year) or a path."""
    if YEAR_PATTERN.fullmatch(argument):
        snapshot = latest_snapshot(current.parent, year=argument)
        if snapshot is None:
            raise FileNotFoundError(f"no snapshot from {argument} next to {current.name}")
        return snapshot
    return resolve_snapshot(Path(argument))


def run_command(
    line: str, graph: PreferenceGraph, resolver: LabelResolver, top: int
) -> tuple[PreferenceGraph, int, bool]:
    """Handle one `:command`. Returns the (possibly new) graph, top and keep-going."""
    command, _, argument = line.partition(" ")
    command, argument = command.lower(), argument.strip()

    if command in (":quit", ":q", ":exit"):
        return graph, top, False

    if command == ":help":
        print(HELP)
    elif command == ":top":
        if argument.isdigit() and int(argument) > 0:
            top = int(argument)
            print(f"Showing the top {top} per direction.")
        else:
            print("Usage: :top N   (N a positive integer)")
    elif command == ":snapshots":
        for sibling in complete_snapshots(graph.path.parent):
            marker = "*" if sibling == graph.path else " "
            print(f" {marker} {sibling.name}  ({sibling.stat().st_size / 1e6:.1f} MB)")
    elif command == ":load":
        if not argument:
            print("Usage: :load YEAR|PATH")
        else:
            try:
                snapshot = resolve_load_argument(graph.path, argument)
                # Stay on the current run where that snapshot has it, else take the latest.
                run_id = graph.mass.run_id if graph.mass is not None else None
                if run_id not in mass_files(snapshot):
                    run_id = None
                graph = PreferenceGraph(snapshot, run_id)
            except (FileNotFoundError, ValueError) as error:
                print(f"Could not load: {error}")
    elif command == ":run":
        if argument.isdigit():
            graph.mass = load_mass(graph.path, int(argument))
        else:
            print(f"Usage: :run ID   (runs with mass for this snapshot: {sorted(mass_files(graph.path)) or 'none'})")
    elif command == ":labels":
        if argument in ("on", "off"):
            resolver.fetch = argument == "on"
            print(f"Wikidata label lookups {argument}.")
        else:
            print("Usage: :labels on|off")
    else:
        print(f"Unknown command {command}. Try :help.")

    return graph, top, True


def interactive(graph: PreferenceGraph, resolver: LabelResolver, top: int) -> None:
    print(f"\nLoaded {graph.path.name}. {HELP}")
    while True:
        try:
            line = input("\ndomain> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not line:
            continue
        if line.startswith(":"):
            graph, top, keep_going = run_command(line, graph, resolver, top)
            if not keep_going:
                return
            continue

        try:
            report(graph, resolver, line, top)
        except KeyboardInterrupt:
            print("\nInterrupted.")


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Explore a reference preference graph snapshot: for a domain, the "
            "heaviest domains preferred over it and the heaviest domains it was "
            "preferred over."
        )
    )
    parser.add_argument(
        "path",
        type=Path,
        help=(
            "a preference-graph edge list (*.csv.gz), or a directory of them, in "
            "which case the newest snapshot is loaded"
        ),
    )
    parser.add_argument(
        "--top",
        type=int,
        default=DEFAULT_TOP,
        help=f"rows per direction (default: {DEFAULT_TOP})",
    )
    parser.add_argument(
        "--domain",
        action="append",
        default=None,
        metavar="DOMAIN",
        help="report this domain and exit instead of prompting; repeatable",
    )
    parser.add_argument(
        "--run-id",
        type=int,
        default=None,
        help="run whose exported domain mass to show (default: the latest exported run)",
    )
    parser.add_argument(
        "--no-fetch-qid-labels",
        dest="fetch_qid_labels",
        action="store_false",
        help="never call the Wikidata API; cached labels are still used",
    )
    parser.add_argument(
        "--label-cache",
        type=Path,
        default=DEFAULT_LABEL_CACHE,
        help=f"QID label cache file (default: {DEFAULT_LABEL_CACHE.name} next to this script)",
    )
    parser.add_argument(
        "--lang",
        default=DEFAULT_LANG,
        help=f"label language (default: {DEFAULT_LANG})",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help=f"seconds per Wikidata request (default: {DEFAULT_TIMEOUT})",
    )
    parser.add_argument("--log-level", default="INFO")

    args = parser.parse_args(argv)
    if args.top < 1:
        parser.error("--top must be >= 1")
    if args.timeout < 1:
        parser.error("--timeout must be >= 1")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s.%(msecs)03d %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[logging.StreamHandler(sys.stdout)],
    )

    try:
        graph = PreferenceGraph(resolve_snapshot(args.path), args.run_id)
    except (FileNotFoundError, ValueError) as error:
        LOGGER.error("%s", error)
        return 1

    resolver = LabelResolver(
        args.label_cache,
        fetch=args.fetch_qid_labels,
        lang=args.lang,
        timeout=args.timeout,
    )

    if args.domain:
        for domain in args.domain:
            report(graph, resolver, domain, args.top)
        return 0

    interactive(graph, resolver, args.top)
    return 0


if __name__ == "__main__":
    sys.exit(main())
