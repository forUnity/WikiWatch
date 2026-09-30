# experimental version that was not properly tested.
from __future__ import annotations

from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

from typing import TYPE_CHECKING

import logging
import time
import numpy as np
import pandas as pd

from metrics.reference_domain_counts import (
    ALLOWED_REFERENCE_PREFIXES,
    ReferenceCountPerDomain,
    all_creates_query,
    domains_from_references,
    normalize_reference_column,
)

# Imported for the annotation only: the package directory is named "statistics" and shadows the
# stdlib module, so a runtime import would force every consumer to load this module by path.
if TYPE_CHECKING:
    from statistics.reference_trustworthiness_session_flow_counts import (
        ReferenceTrustworthinessSessionFlowCounts,
    )

log = logging.getLogger(__name__)

# The numeric value each class is written as in the per-domain result metric. Kept local rather than
# imported from the supermajority classifier so that deleting this file leaves nothing dangling.
CLASSIFICATION_WRITE_VALUES = {
    "dominated": -1.0,
    "contested": 0.0,
    "dominating": 1.0,
    "unclassified": 2.0,
}

# The knee of the evidence discount: below it log1p(m/ref) ~ 0 and DEFF ~ 1, so small domains are
# judged by the plain test. It is entangled with mass_lambda as knee-versus-slope, so sweep one.
MASS_REFERENCE = 1000.0
# Keeps p0, and with it the decision bar, off the 0/1 boundary when a domain has creates but no
# retained mass or the other way round.
P0_CLAMP = 0.02
# Add-one smoothing on both shares, divided by the row count so that a domain with no mass and no
# creates lands on exactly 0.5 at any frame size.
P0_SMOOTHING = 1.0


def wilson_bounds(successes, trials, z: float):
    """Wilson score interval, vectorized and clipped to [0, 1]. trials <= 0 yields (0, 1)."""
    k = np.atleast_1d(np.asarray(successes, dtype="float64"))
    n = np.atleast_1d(np.asarray(trials, dtype="float64"))
    with np.errstate(divide="ignore", invalid="ignore"):
        p = np.divide(k, n, out=np.full(n.shape, 0.5), where=n > 0)
        denominator = 1.0 + z * z / n
        centre = (p + z * z / (2.0 * n)) / denominator
        spread = np.maximum(p * (1.0 - p) / n + z * z / (4.0 * n * n), 0.0)
        half = z * np.sqrt(spread) / denominator
    return (
        np.where(n > 0, np.clip(centre - half, 0.0, 1.0), 0.0),
        np.where(n > 0, np.clip(centre + half, 0.0, 1.0), 1.0),
    )


def _share(values: np.ndarray) -> np.ndarray:
    total = values.sum()
    return values / total if total > 0 else np.zeros_like(values)


def classify_flow(out, inn, mass, creates, *, majority: float, z: float, mass_lambda: float):
    """One row per domain with preference evidence -> (labels, deff, p0, p_dom).

    Wilson score test of the out-share against the threshold, so that evidence strength decides
    instead of a hard guard. The intermediates come back for the diagnostic series.
    """
    out = np.atleast_1d(np.asarray(out, dtype="float64"))
    inn = np.atleast_1d(np.asarray(inn, dtype="float64"))
    mass = np.atleast_1d(np.asarray(mass, dtype="float64"))
    creates = np.atleast_1d(np.asarray(creates, dtype="float64"))
    n = out + inn
    positive_mass = np.maximum(mass, 0.0)

    # Preference pairs are a per-session cross product, so the nominal n overstates how many
    # independent observations a domain contributed, and by more the more sessions it appears in.
    # DEFF is Kish's design effect: a declared, unestimated proxy for that redundancy.
    deff = 1.0 + mass_lambda * np.log1p(positive_mass / MASS_REFERENCE)
    lo, hi = wilson_bounds(out / deff, n / deff, z)

    # Exposure null: being replaced is an opportunity that scales with a domain's installed base, so
    # the neutral out-share is its share of mass against its share of creates rather than 0.5.
    rows = max(n.size, 1)
    m_share = _share(positive_mass) + P0_SMOOTHING / rows
    c_share = _share(creates) + P0_SMOOTHING / rows
    p0 = np.clip(m_share / (m_share + c_share), P0_CLAMP, 1.0 - P0_CLAMP)

    tilted = (majority / (1.0 - majority)) * (p0 / (1.0 - p0))
    # Flooring at majority keeps exposure one-directional -- it may raise the bar, never lower it.
    # Without the floor a clamped p0 admits "dominated" off a point estimate well below 0.5.
    p_dom = np.maximum(tilted / (1.0 + tilted), majority)
    # The dominating side is deliberately left uncorrected: gross creates contain the in-events
    # themselves, so correcting the in-signal by that count would regress the signal on itself.
    p_dng = 1.0 - majority

    labels = np.full(n.shape, "contested", dtype="<U12")
    labels[lo >= p_dom] = "dominated"
    labels[hi <= p_dng] = "dominating"
    labels[n <= 0] = "unclassified"
    return labels, deff, p0, p_dom


class ReferenceTrustworthinessFlowClassificationWilson(Metric):
    """Classifies reference domains by a Wilson score test on the share of preferences flowing out.

    out_weight counts how often a domain was replaced, so out/(out+in) is the evidence of being
    poor. Compared to the threshold through a confidence bound rather than a guard plus a point
    vote, so one unanimous replacement and two hundred are not the same claim. Diverges from the
    supermajority rule it runs beside in dropping the evidence guard, so "unclassified" is
    structurally empty unless the mass table is unusable.

      majority      decision threshold, same meaning and default as the supermajority rule
      z             one-sided confidence level (1.2816 / 1.645 / 2.326 = 90 / 95 / 99%)
      mass_lambda   evidence-discount slope above MASS_REFERENCE; 0.0 is plain Wilson at every mass
    """

    metric_name = "flow_classification_wilson"

    def __init__(
        self,
        datahandler: DataHandler,
        count_of_references_per_domain: ReferenceCountPerDomain,
        flow_counter: "ReferenceTrustworthinessSessionFlowCounts",
        majority: float = 0.7,
        z: float = 1.645,
        mass_lambda: float = 1.0,
        top_largest_domains_to_write: int = 1000,
    ):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = [count_of_references_per_domain, flow_counter]
        self.count_of_references_per_domain = count_of_references_per_domain
        self.flow_counter = flow_counter

        # Load-bearing: it is what makes p_dom >= majority > 1 - majority = p_dng, so the two
        # rejection regions stay disjoint. At 0.5 a tie would satisfy both.
        if not 0.5 < majority <= 1.0:
            raise ValueError("majority must be in (0.5, 1.0].")
        if z <= 0:
            raise ValueError("z must be positive.")
        if mass_lambda < 0:
            raise ValueError("mass_lambda must not be negative.")

        self.majority = majority
        self.z = z
        self.mass_lambda = mass_lambda
        self.largest_domains_to_write_metric = top_largest_domains_to_write

        # Cumulative gross CREATE count per domain, this metric's own state: the counts metric keeps
        # only net mass and discards creates_by_domain as soon as it has subtracted the deletes.
        self.gross_creates = pd.Series(dtype="float64", name="creates")
        self.gross_creates.index.name = "domain"

        self.labels_by_domain = pd.Series(dtype="object")
        self._ref_per_domain: pd.DataFrame | None = None
        self._reset_results()

    def _reset_results(self):
        self.dominated_fraction = self.dominating_fraction = self.contested_fraction = 0.0
        self.unclassified_fraction = 1.0
        self.active_domains = 0
        self.median_deff = self.mass_weighted_mean_deff = 1.0
        self.mass_weighted_mean_p0 = 0.5
        self.exposure_raised_bar_domains = 0

    def calculate_diff(self) -> pd.Series:
        """This timestep's gross CREATE count per domain -- the c of the exposure null."""
        creates = self.datahandler.query_duckdb_df(all_creates_query)
        empty = pd.Series(dtype="float64")
        empty.index.name = "domain"
        if creates is None or creates.empty:
            return empty

        # Filtered exactly as ReferenceCountPerDomain does, with its helpers, so that c is drawn
        # from the same rows and the same domain vocabulary as the mass it is compared against.
        frame = creates.copy()
        frame["reference"] = normalize_reference_column(frame["reference"])
        frame = frame[frame["reference"].str.startswith(ALLOWED_REFERENCE_PREFIXES)]
        if frame.empty:
            return empty

        frame["domain"] = domains_from_references(frame["reference"])
        return frame.groupby("domain")["occurrences"].sum().astype("float64")

    def calculate(self):
        calculate_started = time.perf_counter()
        # Accumulated ahead of the guard below: a timestep whose mass table is unusable still had
        # creates, and dropping them would desynchronise c from the mass it is compared against.
        self.gross_creates = self.gross_creates.add(self.calculate_diff(), fill_value=0.0)

        ref_per_domain = self.count_of_references_per_domain.get_last_value()
        if (
            ref_per_domain is None
            or ref_per_domain.empty
            or "domain" not in ref_per_domain.columns
            or "count" not in ref_per_domain.columns
        ):
            # The series must still be emitted, or the metric silently has a hole this timestep.
            print("No reference counts per domain available in this timestep.")
            self._reset_results()
            self.labels_by_domain = pd.Series(dtype="object")
            self._ref_per_domain = None
            self.write_result()
            return

        self._ref_per_domain = ref_per_domain
        mass = ref_per_domain.set_index("domain")["count"].astype("float64")

        # Trim to whatever the counts metric still retains. That inherits its 360-day tail discard
        # instead of inventing a second policy, and keeps memory bounded. Sum(c) is therefore over
        # the retained index, which is fine because p0 is a ratio of shares over that same
        # population; a domain that drops out restarts its creates, exactly as its mass does.
        self.gross_creates = self.gross_creates.reindex(mass.index).fillna(0.0)

        in_weight, out_weight = self.flow_counter.get_last_value()
        # Indexing on the domains with preference evidence makes n >= 1 structural, so create-only
        # domains cannot flood "contested" with rows that carry no evidence at all.
        domains = pd.Index(sorted(set(in_weight) | set(out_weight)), name="domain", dtype="object")

        def column(counter) -> np.ndarray:
            series = pd.Series(counter, dtype="float64")
            return series.reindex(domains).fillna(0.0).to_numpy()

        # fillna is load-bearing: a NaN compares False everywhere and would silently pass a domain
        # through to "contested" rather than being scored on its real mass.
        mass_values = mass.reindex(domains).fillna(0.0).to_numpy()
        labels, deff, p0, p_dom = classify_flow(
            column(out_weight),
            column(in_weight),
            mass_values,
            self.gross_creates.reindex(domains).fillna(0.0).to_numpy(),
            majority=self.majority,
            z=self.z,
            mass_lambda=self.mass_lambda,
        )
        self.labels_by_domain = pd.Series(labels, index=domains, dtype="object")

        # Raw, unclamped mass in the denominator and the sums, exactly as the supermajority rule
        # does, so the two metrics' series are comparable timestep for timestep.
        total_refs = float(ref_per_domain["count"].sum())
        mass_per_class = pd.Series(mass_values, index=domains).groupby(self.labels_by_domain).sum()

        def class_fraction(class_name: str) -> float:
            if total_refs <= 0:
                return 0.0
            return float(mass_per_class.get(class_name, 0.0)) / total_refs

        self.dominated_fraction = class_fraction("dominated")
        self.dominating_fraction = class_fraction("dominating")
        self.contested_fraction = class_fraction("contested")
        # Everything else is unclassified: overwhelmingly domains that never appeared in a preference.
        classified = self.dominated_fraction + self.dominating_fraction + self.contested_fraction
        self.unclassified_fraction = max(1.0 - classified, 0.0)

        positive_mass = np.maximum(mass_values, 0.0)
        mass_weight = positive_mass.sum()
        self.active_domains = len(domains)
        self.median_deff = float(np.median(deff)) if deff.size else 1.0
        self.mass_weighted_mean_deff = float((deff * positive_mass).sum() / mass_weight) if mass_weight > 0 else 1.0
        self.mass_weighted_mean_p0 = float((p0 * positive_mass).sum() / mass_weight) if mass_weight > 0 else 0.5
        self.exposure_raised_bar_domains = int((p_dom > self.majority).sum())

        print(f"Fraction of references that are clearly dominated: {self.dominated_fraction:.2%}")
        print(f"Fraction of references that are clearly dominating: {self.dominating_fraction:.2%}")
        print(f"Fraction of references that are contested: {self.contested_fraction:.2%}")
        print(f"Fraction of references that are unclassified: {self.unclassified_fraction:.2%}")

        self.write_result()
        log.info(
            "[%s.calculate] total=%.2fs domains=%d median_deff=%.2f",
            self.metric_name,
            time.perf_counter() - calculate_started,
            self.active_domains,
            self.median_deff,
        )

    def write_result(self):
        # write_global_metric_value rejects a Python int, so every value goes through float().
        # The suffix carries its own leading underscore.
        def write(suffix: str, value: float):
            self.datahandler.write_global_metric_value(f"{self.metric_name}{suffix}", float(value))

        write("_dominated", self.dominated_fraction)
        write("_dominating", self.dominating_fraction)
        write("_contested", self.contested_fraction)
        write("_unclassified", self.unclassified_fraction)

        write("_active_domains", self.active_domains)
        # The median says whether the typical domain is discounted, the mass-weighted mean whether
        # the mass is. Together they answer whether mass_lambda is biting or inert on real data.
        write("_median_deff", self.median_deff)
        write("_mass_weighted_mean_deff", self.mass_weighted_mean_deff)
        # The only direct measure of what the exposure null contributes; if p0 sits near 0.5 and no
        # domain has its bar raised, the correction can be dropped along with the gross-creates state.
        write("_mass_weighted_mean_p0", self.mass_weighted_mean_p0)
        write("_exposure_raised_bar_domains", self.exposure_raised_bar_domains)

        self.write_top_domain_classification_results()

    def write_top_domain_classification_results(self):
        if self.largest_domains_to_write_metric <= 0 or self._ref_per_domain is None:
            return

        top_domains = (
            self._ref_per_domain[["domain", "count"]]
            .dropna(subset=["domain"])
            .nlargest(self.largest_domains_to_write_metric, "count")
            .copy()
        )
        if top_domains.empty:
            return

        # A domain missing from the labels had no preference evidence this timestep.
        top_domains["classification_value"] = (
            top_domains["domain"]
            .map(self.labels_by_domain)
            .fillna("unclassified")
            .map(CLASSIFICATION_WRITE_VALUES)
        )
        self.datahandler.write_metric_on_string(
            self.metric_name + "_domain_classification", top_domains, "domain", "classification_value"
        )

    def log_memory(self):
        log_memory_snapshot(
            self.metric_name,
            {
                "gross_creates_rows": self.gross_creates,
                "labels_rows": self.labels_by_domain,
            },
        )

    def save_cache(self):
        print(
            "Warning: (ReferenceTrustworthinessFlowClassificationWilson) save_cache is not "
            "implemented: the cumulative gross creates are lost on restart, so a run cannot be "
            "resumed. Resuming would also need ReferenceCountPerDomain.save_cache."
        )
