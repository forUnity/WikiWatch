"""Parameter tuning details for ReferenceTrustworthinessFlowClassification.

Loads a preference graph snapshot and its exported domain mass exactly like
query_preference_graph.py, and replays the evidence guard of the flow classifier
(metrics/reference_trustworthiness_flow_classification.py on the
reference_quality_further_work branch) for a grid of parameters.

The classifier reads, per domain with a mass,

    out_weight  how often the domain was replaced     (sum of its outgoing edge weights)
    in_weight   how often the domain replaced another (sum of its incoming edge weights)

and leaves a domain unclassified when

    total_weight = in_weight + out_weight <= 0  or  mass <= 0                    (no evidence)
    total_weight < min(threshold * mass, max_threshold_mass)                     (below threshold)

Both counts come from the same session elicitation that builds the graph, so the
edge weights of a snapshot are the counts the classifier sees at that timestep,
given the same session length.  Domains without a mass (discarded into the tail)
are never looked at by the classifier and are only reported for context.

Usage:
    python preference_graph_details/flow_classification_tuning.py preference_graphs
    python preference_graph_details/flow_classification_tuning.py \\
        preference_graphs/preference_graph_2025-08-22.csv.gz \\
        --threshold 0.01 0.05 0.1 --max-threshold-mass 100 1000 none
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd

from query_preference_graph import PreferenceGraph, percent, resolve_snapshot, shorthand

LOGGER = logging.getLogger(__name__)

# The defaults of ReferenceTrustworthinessFlowClassification, marked in the output.
METRIC_THRESHOLD = 0.05
METRIC_MAX_THRESHOLD_MASS = 1000.0

DEFAULT_THRESHOLDS = [0.001, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2]
DEFAULT_MAX_THRESHOLD_MASSES = [METRIC_MAX_THRESHOLD_MASS, math.inf]


def domain_evidence(graph: PreferenceGraph) -> pd.DataFrame:
    """Per domain with a mass: its mass and in/out/total preference weight."""
    if graph.mass is None:
        raise ValueError(f"no domain mass loaded for {graph.path.name}; the guard needs it")

    node_count = len(graph.names)
    weights = graph.weight.astype(float)
    per_node = pd.DataFrame(
        {
            "out_weight": np.bincount(graph.src, weights=weights, minlength=node_count),
            "in_weight": np.bincount(graph.dst, weights=weights, minlength=node_count),
        },
        index=pd.Index(graph.names, name="domain"),
    )

    mass = pd.Series(graph.mass.count, name="mass", dtype=float)
    mass.index.name = "domain"
    evidence = per_node.reindex(mass.index, fill_value=0.0)
    evidence.insert(0, "mass", mass)
    evidence["total_weight"] = evidence["in_weight"] + evidence["out_weight"]
    return evidence


def has_evidence(evidence: pd.DataFrame) -> pd.Series:
    """Domains the classifier can classify at all, whatever the parameters."""
    return (evidence["total_weight"] > 0.0) & (evidence["mass"] > 0.0)


def guard_outcome(evidence: pd.DataFrame, threshold: float, max_threshold_mass: float) -> pd.Series:
    """'no evidence', 'below threshold' or 'classified' per domain, as the classifier decides it."""
    no_evidence = ~has_evidence(evidence)
    required = np.minimum(threshold * evidence["mass"], max_threshold_mass)
    below = ~no_evidence & (evidence["total_weight"] < required)

    outcome = pd.Series("classified", index=evidence.index)
    outcome[below] = "below threshold"
    outcome[no_evidence] = "no evidence"
    return outcome


def print_context(graph: PreferenceGraph, evidence: pd.DataFrame, exclude_no_evidence: bool = False) -> None:
    mass = graph.mass
    in_graph = evidence["total_weight"] > 0
    graph_without_mass = len(graph.names) - int(in_graph.sum())

    print()
    print("=" * 78)
    print(f"snapshot    {graph.path.name}")
    print(f"mass        run {mass.run_id}, {len(evidence):,} domains, {shorthand(evidence['mass'].sum())} references")
    print(
        f"evidence    {int(in_graph.sum()):,} domains with mass have preferences; "
        f"{graph_without_mass:,} graph domains have no mass (tail, ignored)"
    )
    if exclude_no_evidence:
        print_excluded_no_evidence(evidence)
    print(f"* = metric default (threshold {METRIC_THRESHOLD}, max_threshold_mass {METRIC_MAX_THRESHOLD_MASS:g})")
    print("=" * 78)


def print_excluded_no_evidence(evidence: pd.DataFrame) -> None:
    """Summarise the domains dropped by --exclude-no-evidence."""
    no_evidence = ~has_evidence(evidence)
    count = int(no_evidence.sum())
    print(
        f"excluded    {count:,} domains without evidence (no preferences or mass <= 0): "
        f"{percent(count, len(evidence))} of domains, "
        f"{percent(evidence.loc[no_evidence, 'mass'].sum(), evidence['mass'].sum())} of mass"
    )
    print(
        f"table       relative to the {len(evidence) - count:,} classifiable domains "
        f"and their {shorthand(evidence.loc[~no_evidence, 'mass'].sum())} references"
    )


def print_threshold_table(
    evidence: pd.DataFrame,
    thresholds: list[float],
    max_threshold_masses: list[float],
    exclude_no_evidence: bool = False,
) -> None:
    """Classified vs. unclassified domains (and their reference mass) per parameter pair.

    Fractions are relative to the domains in `evidence` and their summed mass, which is
    the `total_refs` the classifier divides by.  With `exclude_no_evidence` the caller
    has already dropped the domains without evidence, so the fractions are of the
    theoretically classifiable domains and mass, and the no-evidence column is omitted.
    """
    total_mass = evidence["mass"].sum()
    # Without the no-evidence domains, every unclassified domain is below the threshold.
    headers = ["", "threshold", "max_mass", "classified", "unclassified"]
    if not exclude_no_evidence:
        headers += ["below thr", "no evid."]
    headers += ["classified mass", "unclassified mass"]
    rows = []
    for max_threshold_mass in max_threshold_masses:
        for threshold in thresholds:
            outcome = guard_outcome(evidence, threshold, max_threshold_mass)
            counts = outcome.value_counts()
            classified = int(counts.get("classified", 0))
            classified_mass = evidence.loc[outcome == "classified", "mass"].sum()
            is_default = threshold == METRIC_THRESHOLD and max_threshold_mass == METRIC_MAX_THRESHOLD_MASS
            row = [
                "*" if is_default else "",
                f"{threshold:g}",
                "none" if math.isinf(max_threshold_mass) else f"{max_threshold_mass:g}",
                f"{classified:,} ({percent(classified, len(evidence))})",
                f"{len(evidence) - classified:,}",
            ]
            if not exclude_no_evidence:
                row += [f"{int(counts.get('below threshold', 0)):,}", f"{int(counts.get('no evidence', 0)):,}"]
            row += [
                percent(classified_mass, total_mass),
                percent(total_mass - classified_mass, total_mass),
            ]
            rows.append(row)

    widths = [max(len(header), *(len(row[index]) for row in rows)) for index, header in enumerate(headers)]

    def line(cells: list[str]) -> str:
        return " ".join(cell.rjust(width) for cell, width in zip(cells, widths))

    print()
    print("CLASSIFIED VS. UNCLASSIFIED DOMAINS")
    print(line(headers))
    for row in rows:
        print(line(row))


def parse_max_threshold_mass(value: str) -> float:
    if value.lower() in ("none", "inf"):
        return math.inf
    return float(value)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Show how the flow classification parameters split domains into classified and unclassified."
    )
    parser.add_argument(
        "path",
        type=Path,
        help="a preference-graph edge list (*.csv.gz), or a directory of them, in which case the newest is loaded",
    )
    parser.add_argument("--run-id", type=int, default=None, help="run whose exported domain mass to use (default: latest)")
    parser.add_argument(
        "--threshold",
        type=float,
        nargs="+",
        default=DEFAULT_THRESHOLDS,
        help=f"threshold values to evaluate (default: {' '.join(map(str, DEFAULT_THRESHOLDS))})",
    )
    parser.add_argument(
        "--max-threshold-mass",
        type=parse_max_threshold_mass,
        nargs="+",
        default=DEFAULT_MAX_THRESHOLD_MASSES,
        help="max_threshold_mass values to evaluate; 'none' disables the cap (default: 1000 none)",
    )
    parser.add_argument(
        "--exclude-no-evidence",
        action="store_true",
        help=(
            "only summarise the domains without any preferences, and give the table's "
            "fractions relative to the theoretically classifiable domains and their mass"
        ),
    )
    parser.add_argument("--log-level", default="INFO")
    return parser.parse_args(argv)


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
        evidence = domain_evidence(graph)
    except (FileNotFoundError, ValueError) as error:
        LOGGER.error("%s", error)
        return 1

    print_context(graph, evidence, args.exclude_no_evidence)
    if args.exclude_no_evidence:
        evidence = evidence[has_evidence(evidence)]
    print_threshold_table(evidence, args.threshold, args.max_threshold_mass, args.exclude_no_evidence)
    return 0


if __name__ == "__main__":
    sys.exit(main())
