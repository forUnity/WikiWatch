from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

from collections import Counter
from contextlib import contextmanager

import logging
import time
import numpy as np
import pandas as pd
import datetime

log = logging.getLogger(__name__)


# revision_id breaks timestamp ties so a rerun over the same data produces the same sessions.
# ref_property_id is deliberately not selected: nothing downstream reads it, and at the row
# counts of a full timeslice an unused column is several hundred MB of transfer and residency.
# revision_id has to stay in the select list because ORDER BY on a UNION may only reference
# output columns; it is dropped again as soon as the rows arrive.
all_edits_query="""--sql
select timestamp, revision_id, action, new_value as reference, entity_id, property_id, value_id from reference_change
WHERE "action" = 'CREATE' and "change_target" = ''
UNION ALL
select timestamp, revision_id, action, old_value as reference, entity_id, property_id, value_id from reference_change
WHERE "action" = 'DELETE' and "change_target" = ''
ORDER BY timestamp ASC, revision_id ASC
"""

from metrics.reference_domain_counts import (
    ReferenceCountPerDomain
)
from statistics.reference_trustworthiness_session_flow_counts import ReferenceTrustworthinessSessionFlowCounts

# The numeric value each class is written as in the per-domain result metric. "unclassified" is
# off the dominated..dominating axis, so it sits outside that range rather than between its levels.
CLASSIFICATION_WRITE_VALUES = {
    "dominated": -1.0,
    "contested": 0.0,
    "dominating": 1.0,
    "unclassified": 2.0,
}

class ReferenceTrustworthinessFlowClassification(Metric):
    """Classifies reference domains from the preferences the community expresses when editing references.

    Sessions of changes to one statement yield pairwise preferences, which accumulate into a
    two counts per domain.

      session_builder                  "vectorized" or "loop"; identical semantics, the loop is
                                       the readable reference implementation

      session length                    timedelta; the maximum gap between edits to the same statement.
    """
    metric_name = "flow_classification_supermajority"
    def __init__(
        self,
        datahandler : DataHandler,
        count_of_references_per_domain : ReferenceCountPerDomain,
        flow_counter : ReferenceTrustworthinessSessionFlowCounts,
        majority : float = 0.6,
        threshold : float = 0.05,
        top_largest_domains_to_write : int = 1000,
    ):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = [ count_of_references_per_domain, flow_counter ]
        self.count_of_references_per_domain = count_of_references_per_domain
        self.flow_counter = flow_counter

        self.majority = majority
        self.threshold = threshold
        # self.max_threshold_mass = max_threshold_mass
        self.largest_domains_to_write_metric = top_largest_domains_to_write

    @contextmanager
    def _timed(self, phase: str):
        """Accumulate wall time for one phase of the timestep.

        Two perf_counter reads per phase and a handful of phases per timestep, so the
        measurement is orders of magnitude cheaper than anything it measures. Phases are
        reported in insertion order, which is execution order.
        """
        started = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - started
            self.phase_seconds_last_step[phase] = (
                self.phase_seconds_last_step.get(phase, 0.0) + elapsed
            )

    def _log_phase_timings(self, total_seconds: float) -> None:
        phases = " ".join(
            f"{phase}={seconds:.2f}s" for phase, seconds in self.phase_seconds_last_step.items()
        )
        log.info("[%s.calculate] total=%.2fs %s", self.metric_name, total_seconds, phases)

    def calculate_diff(self):
        # Everything this metric reads comes from its dependencies; there is no per-slice diff.
        return None

    def calculate(self):
        calculate_started = time.perf_counter()
        self.phase_seconds_last_step = {}

        with self._timed("classify"):
            # get results of counts per domain
            ref_per_domain = self.count_of_references_per_domain.get_last_value()
            if (
                ref_per_domain is None
                or ref_per_domain.empty
                or "domain" not in ref_per_domain.columns
                or "count" not in ref_per_domain.columns
            ):
                # The series must still be emitted, or the metric silently has a hole this timestep.
                print("No reference counts per domain available in this timestep.")
                self.dominated_fraction = self.dominating_fraction = self.contested_fraction = 0.0
                self.unclassified_fraction = 1.0
                self.write_result({}, pd.DataFrame(columns=["domain", "count"]))
                return
            mass_per_domain_dict = dict(zip(ref_per_domain["domain"], ref_per_domain["count"]))
            self.in_weight_per_domain, self.out_weight_per_domain = self.flow_counter.get_last_value()

            dominated_domains = set()
            dominating_domains = set()
            contested_domains = set()

            for domain, mass in mass_per_domain_dict.items():
                in_weight = self.in_weight_per_domain.get(domain, 0.0)
                out_weight = self.out_weight_per_domain.get(domain, 0.0)

                total_weight = in_weight + out_weight

                if total_weight <= 0.0 or mass <= 0.0:
                    continue

                if total_weight < self.threshold * mass:
                    continue

                # out_weight counts how often the domain was replaced, so it is the evidence of
                # being poor: dominated iff out/(out+in) >= majority.
                if out_weight / total_weight >= self.majority:
                    dominated_domains.add(domain)
                elif in_weight / total_weight >= self.majority:
                    dominating_domains.add(domain)
                else:
                    contested_domains.add(domain)

        with self._timed("score"):
            total_refs = ref_per_domain["count"].sum()

            # Domain -> class with explicit precedence (dominating > dominated > contested).
            # Also handed to write_result, which needs the same map per domain.
            class_of_domain = {domain: "contested" for domain in contested_domains}
            class_of_domain.update({domain: "dominated" for domain in dominated_domains})
            class_of_domain.update({domain: "dominating" for domain in dominating_domains})
            mass_per_class = (
                ref_per_domain["count"].groupby(ref_per_domain["domain"].map(class_of_domain)).sum()
            )

            def class_fraction(class_name: str) -> float:
                if total_refs <= 0:
                    return 0.0
                return float(mass_per_class.get(class_name, 0.0)) / float(total_refs)

            self.dominated_fraction = class_fraction("dominated")
            self.dominating_fraction = class_fraction("dominating")
            self.contested_fraction = class_fraction("contested")

            # Everything else is "unclassified": either the domain never appeared in a preference,
            # or it had too little evidence to pass the guard. The old classifier split this into
            # "unclassified" and "not in graph", but that split only existed because tombstoning
            # removed a recycled domain from the graph; there is no graph here.
            classified_fraction = self.dominated_fraction + self.dominating_fraction + self.contested_fraction
            self.unclassified_fraction = max(1.0 - classified_fraction, 0.0)

        print(f"Fraction of references that are clearly dominated: {self.dominated_fraction:.2%}")
        print(f"Fraction of references that are clearly dominating: {self.dominating_fraction:.2%}")
        print(f"Fraction of references that are contested: {self.contested_fraction:.2%}")
        print(f"Fraction of references that are unclassified: {self.unclassified_fraction:.2%}")

        with self._timed("write"):
            self.write_result(
                class_of_domain,
                ref_per_domain,
            )

        self._log_phase_timings(time.perf_counter() - calculate_started)

    def write_result(self, class_of_domain: dict[str, str], ref_per_domain: pd.DataFrame):
        # this string consists of the main settings used
        def write(suffix: str, value: float):
            self.datahandler.write_global_metric_value(f"{self.metric_name}{suffix}", float(value))

        # save our result sets into the result database
        write("_dominated", self.dominated_fraction)
        write("_dominating", self.dominating_fraction)
        write("_contested", self.contested_fraction)
        write("_unclassified", self.unclassified_fraction)

        # writes the domain names and the result for the top domains
        self.write_top_domain_classification_results(class_of_domain, ref_per_domain)


    def write_top_domain_classification_results(
        self,
        class_of_domain: dict[str, str],
        ref_per_domain: pd.DataFrame,
    ):
        if self.largest_domains_to_write_metric <= 0 or ref_per_domain.empty:
            return

        top_domains = (
            ref_per_domain[["domain", "count"]]
            .dropna(subset=["domain"])
            .nlargest(self.largest_domains_to_write_metric, "count")
            .copy()
        )

        if top_domains.empty:
            return

        # class_of_domain already carries the precedence the classes were assigned with; a domain
        # missing from it got no class this timestep and is written as "unclassified". The second
        # map turns the class name into the number written out; a class without a write value
        # (which should not happen) stays NaN and is dropped rather than written as one.
        top_domains["classification_value"] = (
            top_domains["domain"]
            .map(class_of_domain)
            .fillna("unclassified")
            .map(CLASSIFICATION_WRITE_VALUES)
        )

        if top_domains.empty:
            return

        metric_name = self.metric_name + f"_domain_classification"
        self.datahandler.write_metric_on_string(metric_name, top_domains, "domain", "classification_value")


    def log_memory(self):
        # no detailed memory logging implemented, as it is currently not used for the paper results.
        pass

    def save_cache(self):
        print(
            "Warning: (ReferenceReplacement) save_cache is not implemented: the preference graph, "
            "the open sessions and the tombstone state are lost on restart, so a run cannot be "
            "resumed. Resuming would also need ReferenceCountPerDomain.save_cache."
        )
