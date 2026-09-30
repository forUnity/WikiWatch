from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

from collections import Counter
from contextlib import contextmanager

import logging
import math
import os
import time
import numpy as np
import pandas as pd
import datetime
from graph_tool import Graph, GraphView
from graph_tool.topology import label_components, shortest_distance

log = logging.getLogger(__name__)

# Local scratch output (plots, ad-hoc CSV exports). Gitignored, so it does not exist in a fresh
# job snapshot and every writer here has to create it first.
OUTPUT_DIR = "reference_ranking_misc"

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
    ALLOWED_REFERENCE_PREFIXES,
    INVALID_URL_DOMAIN,
    ReferenceCountPerDomain,
    domain_from_reference,
    normalize_reference_column,
)


def preferences_from_session_counts(reference_counts: dict[int, tuple[str, int]]) -> list[tuple[str, str]]:
    """Elicit the preference set P_s of one concluded session.

    `reference_counts` maps a reference to its (domain, net_count), where net_count is the number
    of times the reference was added minus the number of times it was removed during the session.
    Following the Edit Session Preferences definition, `current` holds the references added more
    often than removed and `edited \\ current` holds the rest, giving

        P_s = { dom(a) < dom(b) | a in (edited \\ current), b in current }.

    Self-preferences dom(a) < dom(a) carry no information and are discarded here. Note that a
    domain can legitimately appear on both sides: one of its references may survive the session
    while another is replaced.
    """
    current_domains: set[str] = set()
    replaced_domains: set[str] = set()
    for domain, net_count in reference_counts.values():
        if net_count > 0:
            current_domains.add(domain)
        else:
            replaced_domains.add(domain)

    return [
        (replaced_domain, preferred_domain)
        for replaced_domain in replaced_domains
        for preferred_domain in current_domains
        if replaced_domain != preferred_domain
    ]


def quantile_weight_cutoff(weights, fraction: float | None) -> int | None:
    """Smallest weight c such that discarding every edge with weight <= c drops >= `fraction` of edges.

    The index is ceil(fraction * E) - 1 rather than int(fraction * E): the latter overshoots by one
    whenever fraction * E is an integer. For weights [1]*5 + [2]*95 at fraction=0.05, int() picks
    c = 2 and wipes out every edge, while the correct c = 1 drops exactly the five weight-1 edges.

    Because weights are small integers many edges usually share the cutoff value, so discarding
    everything at or below it removes considerably more than `fraction` of the edges. That is
    intended: the quantile only selects *which* weight is still considered noise.
    """
    if fraction is None or fraction <= 0.0:
        return None

    weights = np.asarray(weights)
    if weights.size == 0:
        return None

    index = math.ceil(fraction * weights.size) - 1
    index = min(max(index, 0), weights.size - 1)
    return int(np.partition(weights, index)[index])


def reverse_edge_weights(sources, targets, weights, vertex_count: int):
    """For every edge (s, t), the weight of the backwards edge (t, s), or 0 if it does not exist.

    Relies on the graph holding no parallel edges (guaranteed by _add_or_increment_edge) so that
    the (source, target) key is unique, and on vertex indices being dense (vertices are never
    removed - that is what tombstoning is for) so they can be packed into a single int64 key.
    """
    sources = np.asarray(sources, dtype=np.int64)
    targets = np.asarray(targets, dtype=np.int64)
    weights = np.asarray(weights, dtype=np.int64)
    if sources.size == 0:
        return np.zeros(0, dtype=np.int64)

    keys = sources * vertex_count + targets
    reverse_keys = targets * vertex_count + sources

    order = np.argsort(keys)
    sorted_keys = keys[order]
    positions = np.searchsorted(sorted_keys, reverse_keys)
    safe_positions = np.minimum(positions, sorted_keys.size - 1)
    found = (positions < sorted_keys.size) & (sorted_keys[safe_positions] == reverse_keys)

    return np.where(found, weights[order][safe_positions], 0)


class ReferenceReplacement(Metric):
    """Classifies reference domains from the preferences the community expresses when editing references.

    Sessions of changes to one statement yield pairwise preferences, which accumulate into a
    preference graph. Domains are then classified as dominating / dominated / contested from the
    relevant and clear edges of that graph, and each class is reported as a fraction of total mass.

    Parameters
      classification_method            "simple_with_path" (paper), "simple_degree" or "weight_flow"
      graph_creation_method            "discard_edge_heuristic" (paper) or "keep_all_edges"
      elicitation_method               "session" (paper) or "replacements"
      session_builder                  "vectorized" or "loop"; identical semantics, the loop is
                                       the readable reference implementation
      discard_bottom_edge_fraction     r: an edge is relevant iff its weight exceeds the weight at
                                       the bottom r-quantile (None disables the filter)
      margin                           an edge x->y is clear iff weight(x,y) >= margin * weight(y,x)
      session_length                   idle gap after which a statement's session is concluded
      max_rebuttal_path_length         hop limit when looking for a rebutting path
      low_mass_tombstone_threshold     domains below this mass are marked dead and their vertex reused
      min_graph_size_for_tombstoning   no tombstoning until the graph holds this many live vertices
      top_largest_domains_to_write     how many domains (by mass) get their class written out
      weight_flow_threshold            "decisive" parameter of the flow classification
      weight_flow_min_change_fraction  "flow" parameter of the flow classification

    Writes (all prefixed reference_replacements_{classification}_{graph}_dis_{r}pct):
      _dominated / _dominating / _contested / _unclassified   mass fraction per class
      _not_in_graph                          mass fraction of domains absent from the graph
      _relevance_weight_cutoff               weight at or below which edges were discarded
      _relevant_edge_fraction                accepted edges / all edges
      _avg_session_size_step                 mean changes per session concluded this timeslice
      _avg_session_size_cumulative           mean changes per session over the whole run
      _avg_session_references_cumulative     mean distinct references per session over the whole run
      _closed_sessions_step / _closed_sessions_total
      _graph_edge_density / _graph_average_degree              over the raw graph
      _pref_graph_edge_density / _pref_graph_average_degree    over accepted edges and live vertices
      _pref_graph_max_average_degree         running maximum of the above
      _live_vertices / _tombstoned_vertices
      _recycled_mass_total / _recycled_mass_step / _tombstoned_domains_step
      _domain_classification                 per-domain class of the largest domains
    """
    metric_name = "reference_replacements"
    time_delta_for_expressing_preference = datetime.timedelta(days=1)

    def __init__(
        self,
        datahandler : DataHandler,
        count_of_references_per_domain : ReferenceCountPerDomain,
        classification_method : str = "simple_with_path",
        graph_creation_method : str = "discard_edge_heuristic",
        elicitation_method : str = "session",
        session_builder : str = "vectorized",
        top_largest_domains_to_write: int = 1000,
        low_mass_tombstone_threshold: int = 3,
        min_graph_size_for_tombstoning: int = 4000,
        discard_bottom_edge_fraction: float | None = 0.05,
        margin: float = 0.8,
        session_length: datetime.timedelta = datetime.timedelta(days=2),
        weight_flow_threshold: float = 0.7,
        weight_flow_min_change_fraction: float = 0.01,
        max_rebuttal_path_length: int = 3,
    ):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = [ count_of_references_per_domain ]

        self.count_of_references_per_domain = count_of_references_per_domain

        self.reference_graph = Graph(directed=True)
        self.reference_graph.set_fast_edge_lookup(True)
        self.vertex_domain = self.reference_graph.new_vertex_property("string")
        self.vertex_tombstoned = self.reference_graph.new_vertex_property("bool")
        self.edge_weight = self.reference_graph.new_edge_property("int")
        self.edge_accepted = self.reference_graph.new_edge_property("bool")
        self.reference_graph.vp["domain"] = self.vertex_domain
        self.reference_graph.vp["tombstoned"] = self.vertex_tombstoned
        self.reference_graph.ep["weight"] = self.edge_weight
        self.reference_graph.ep["accepted"] = self.edge_accepted
        self.domain_to_vertex: dict[str, object] = {}
        self.tombstoned_domains: set[str] = set()
        self.tombstoned_domain_mass: dict[str, float] = {}
        self.reusable_vertex_pool: set[int] = set()
        self.low_mass_tombstone_threshold = low_mass_tombstone_threshold
        self.min_graph_size_for_tombstoning = min_graph_size_for_tombstoning
        self.recycled_mass_cumulative = 0.0
        self.recycled_mass_last_step = 0.0
        self.tombstoned_domains_last_step = 0

        # methods and parameters
        self.m_pref_graph_method = graph_creation_method
        if (
            discard_bottom_edge_fraction is not None
            and not 0.0 <= discard_bottom_edge_fraction < 1.0
        ):
            raise ValueError("discard_bottom_edge_fraction must be in [0.0, 1.0).")
        # An edge is relevant iff its weight is above the weight at this bottom quantile.
        self.relevance_bottom_edge_fraction = discard_bottom_edge_fraction
        self.margin = margin
        self.max_rebuttal_path_length = max_rebuttal_path_length

        self.m_classification_method = classification_method
        self.largest_domains_to_write_metric = top_largest_domains_to_write

        if self.m_classification_method == "weight_flow":
            self.m_pref_graph_method = "keep_all_edges"

        # only classify as good / bad if the flow in one direction is at least this fraction of the total
        self.m_weight_flow_threshold = weight_flow_threshold
        # skip classification if there are too few changes compared to the total count for this
        # domain (to avoid classifying based on very little data)
        self.m_weight_flow_min_change_fraction = weight_flow_min_change_fraction

        self.m_elicitation_method = elicitation_method
        self.m_session_death_time = session_length
        if session_builder not in ("vectorized", "loop"):
            raise ValueError("session_builder must be 'vectorized' or 'loop'.")
        self.m_session_builder = session_builder

        # perf accounting: wall time per phase of the timestep, plus how many rebuttal searches
        # the path classifier actually ran (the count the phase timing alone cannot explain).
        self.phase_seconds_last_step: dict[str, float] = {}
        self.rebuttal_searches_last_step = 0
        self.rebuttal_skipped_last_step = 0

        # fun-fact accumulators
        self.closed_sessions_last_step = 0
        self.closed_sessions_total = 0
        self.session_edits_last_step = 0
        self.session_edits_total = 0
        self.session_references_total = 0
        self.pref_graph_max_average_degree = 0.0
        self.relevance_weight_cutoff_last_step = 0.0
        self.relevant_edge_fraction_last_step = 0.0

        if self.m_elicitation_method == "replacements":
            self.last_creates_dict = dict() # key: statement_uid, value: previous_reference
            self.last_create_timestamp_dict = dict()
            self.last_deletes_dict = dict() # key: statement_uid, value: previous_reference
            self.last_delete_timestamp_dict = dict() # key: statement_uid, value: timestamp of last delete
        if self.m_elicitation_method == "session":
            # sessions are per statement. Use statement_uid as key.
            # reference -> (domain, times added minus times removed) for every reference the session touched
            self.open_session_counts: dict[str, dict[int, tuple[str, int]]] = {}
            self.open_session_last_edit: dict[str, datetime.datetime] = {}
            self.open_session_edits: dict[str, int] = {}

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
        log.info(
            "[%s.calculate] total=%.2fs %s rebuttal_searches=%d rebuttal_skipped=%d",
            self.metric_name,
            total_seconds,
            phases,
            self.rebuttal_searches_last_step,
            self.rebuttal_skipped_last_step,
        )

    def _get_or_add_vertex(self, domain: str, mass_per_domain_dict: dict[str, int] | None = None):
        vertex = self.domain_to_vertex.get(domain)
        if vertex is None:
            while self.reusable_vertex_pool:
                vertex_index = self.reusable_vertex_pool.pop()
                vertex = self.reference_graph.vertex(vertex_index)
                if not self.vertex_tombstoned[vertex]:
                    continue
                old_domain = self.vertex_domain[vertex]
                recycled_mass = self.tombstoned_domain_mass.get(old_domain)
                if recycled_mass is None and mass_per_domain_dict is not None:
                    recycled_mass = float(mass_per_domain_dict.get(old_domain, 0))
                self._prepare_recycled_vertex(vertex, domain, recycled_mass or 0.0)
                return vertex

            vertex = self.reference_graph.add_vertex()
            self.vertex_domain[vertex] = domain
            self.vertex_tombstoned[vertex] = False
            self.domain_to_vertex[domain] = vertex
        return vertex

    def _set_vertex_tombstoned(self, vertex, tombstoned: bool):
        self.vertex_tombstoned[vertex] = tombstoned

    def _add_vertex_to_reusable_pool(self, vertex) -> None:
        self.reusable_vertex_pool.add(int(vertex))

    def _remove_vertex_from_reusable_pool(self, vertex) -> None:
        self.reusable_vertex_pool.discard(int(vertex))

    def _remove_incident_edges(self, vertex):
        seen_edges: set[tuple[int, int]] = set()
        for edge in list(vertex.out_edges()) + list(vertex.in_edges()):
            edge_key = (int(edge.source()), int(edge.target()))
            if edge_key in seen_edges:
                continue
            seen_edges.add(edge_key)
            self.reference_graph.remove_edge(edge)

    def _prepare_recycled_vertex(self, vertex, domain: str, recycled_mass: float = 0.0):
        old_domain = self.vertex_domain[vertex]
        if old_domain in self.domain_to_vertex and self.domain_to_vertex[old_domain] == vertex:
            del self.domain_to_vertex[old_domain]
        self.tombstoned_domains.discard(old_domain)
        self.tombstoned_domain_mass.pop(old_domain, None)
        self._remove_vertex_from_reusable_pool(vertex)
        self._remove_incident_edges(vertex)
        self.vertex_domain[vertex] = domain
        self.domain_to_vertex[domain] = vertex
        self._set_vertex_tombstoned(vertex, False)
        self.recycled_mass_last_step += float(recycled_mass)
        self.recycled_mass_cumulative += float(recycled_mass)

    def _get_domain_for_vertex(self, vertex) -> str:
        return self.vertex_domain[vertex]

    def _add_or_increment_edge(self, source_vertex, target_vertex, delta: int):
        if delta <= 0:
            return
        edge = self.reference_graph.edge(source_vertex, target_vertex)
        if edge is None:
            edge = self.reference_graph.add_edge(source_vertex, target_vertex)
            self.edge_weight[edge] = delta
            self.edge_accepted[edge] = False
        else:
            self.edge_weight[edge] += delta

    def _live_vertex_count(self) -> int:
        vertex_count = self.reference_graph.num_vertices()
        if vertex_count == 0:
            return 0
        return int(np.count_nonzero(self.vertex_tombstoned.a[:vertex_count] == 0))

    def _tombstone_low_mass_domains(self, mass_per_domain_dict: dict[str, int]) -> float:
        self.tombstoned_domains_last_step = 0
        if self.low_mass_tombstone_threshold is None or self.low_mass_tombstone_threshold <= 0:
            return 0.0

        if self.min_graph_size_for_tombstoning is not None and self.min_graph_size_for_tombstoning > 0:
            if self._live_vertex_count() < self.min_graph_size_for_tombstoning:
                return 0.0

        newly_tombstoned_mass = 0.0
        for domain, vertex in list(self.domain_to_vertex.items()):
            if self.vertex_tombstoned[vertex]:
                continue
            domain_mass = mass_per_domain_dict.get(domain, 0)
            if domain_mass < self.low_mass_tombstone_threshold:
                self._set_vertex_tombstoned(vertex, True)
                self.tombstoned_domains.add(domain)
                self.tombstoned_domain_mass[domain] = float(domain_mass)
                self._add_vertex_to_reusable_pool(vertex)
                newly_tombstoned_mass += float(domain_mass)
                self.tombstoned_domains_last_step += 1

        return newly_tombstoned_mass

    def _update_tombstone_states(self, mass_per_domain_dict: dict[str, int]):
        if self.low_mass_tombstone_threshold is None or self.low_mass_tombstone_threshold <= 0:
            return

        threshold = self.low_mass_tombstone_threshold
        for domain in list(self.tombstoned_domains):
            vertex = self.domain_to_vertex.get(domain)
            if vertex is None:
                continue
            if mass_per_domain_dict.get(domain, 0) >= threshold:
                self._set_vertex_tombstoned(vertex, False)
                self._remove_vertex_from_reusable_pool(vertex)
                self.tombstoned_domains.discard(domain)
                self.tombstoned_domain_mass.pop(domain, None)

    def _close_session(self, uid) -> list[tuple[str, str]]:
        reference_counts = self.open_session_counts.pop(uid, {})
        edit_count = self.open_session_edits.pop(uid, 0)
        self.open_session_last_edit.pop(uid, None)

        self.closed_sessions_last_step += 1
        self.closed_sessions_total += 1
        self.session_edits_last_step += edit_count
        self.session_edits_total += edit_count
        self.session_references_total += len(reference_counts)

        return preferences_from_session_counts(reference_counts)

    def _sweep_expired_sessions(self, slice_end) -> list[tuple[str, str]]:
        """Conclude every session that saw no further changes before the end of this timeslice."""
        session_death_time = self.m_session_death_time
        expired_uids = [
            uid
            for uid, last_edit in self.open_session_last_edit.items()
            if last_edit + session_death_time < slice_end
        ]
        preferences: list[tuple[str, str]] = []
        for uid in expired_uids:
            preferences.extend(self._close_session(uid))
        return preferences

    def _elicit_preferences_from_sessions(self, edits: pd.DataFrame) -> list[tuple[str, str]]:
        if self.m_session_builder == "loop":
            return self._elicit_preferences_from_sessions_loop(edits)
        return self._elicit_preferences_from_sessions_vectorized(edits)

    def _elicit_preferences_from_sessions_loop(self, edits: pd.DataFrame) -> list[tuple[str, str]]:
        """Reference implementation: one pass over the rows, kept as the semantics of record."""
        preferences: list[tuple[str, str]] = []
        session_death_time = self.m_session_death_time

        for row in edits.itertuples(index=False):
            uid = row.statement_uid
            timestamp = row.timestamp

            # a gap larger than the session length concludes the previous session
            last_edit = self.open_session_last_edit.get(uid)
            if last_edit is not None and last_edit + session_death_time < timestamp:
                preferences.extend(self._close_session(uid))

            reference_counts = self.open_session_counts.get(uid)
            if reference_counts is None:
                reference_counts = {}
                self.open_session_counts[uid] = reference_counts
                self.open_session_edits[uid] = 0

            # Counting per reference (rather than tracking a set of domains) is what makes the
            # session order independent, so ties in the timestamp cannot change the outcome.
            reference_key = row.reference_key
            domain, net_count = reference_counts.get(reference_key, (row.domain, 0))
            reference_counts[reference_key] = (
                domain,
                net_count + int(row.signed),
            )
            self.open_session_edits[uid] += 1
            self.open_session_last_edit[uid] = timestamp

        preferences.extend(self._sweep_expired_sessions(self.datahandler.get_current_end_timestamp()))
        return preferences

    def _elicit_preferences_from_sessions_vectorized(self, edits: pd.DataFrame) -> list[tuple[str, str]]:
        """Same semantics as the loop, but the per-row work happens inside pandas.

        Counting per reference makes a session order independent, which is what allows the whole
        construction to be expressed with groupbys: session boundaries come from the gap to the
        previous change of the same statement, and the preference cross product is a self join.
        """
        preferences: list[tuple[str, str]] = []
        session_death_time = self.m_session_death_time
        slice_end = self.datahandler.get_current_end_timestamp()

        if edits.empty:
            return self._sweep_expired_sessions(slice_end)

        # A stable sort by statement keeps the (timestamp, revision_id) order within each statement.
        edits = edits.sort_values("statement_uid", kind="stable")
        statement_uid = edits["statement_uid"]
        timestamps = edits["timestamp"]
        first_timestamp_per_uid = timestamps.groupby(statement_uid, sort=False).first()

        # Resolve the sessions carried in from earlier timeslices: conclude the ones whose gap to
        # this slice already exceeds the session length, and set the rest aside to be merged into
        # their statement's first session. Afterwards the first row of every statement reliably
        # belongs to session 0, whichever of the two cases applied.
        carried_counts: dict[str, dict[int, tuple[str, int]]] = {}
        carried_edit_counts: dict[str, int] = {}
        for uid in list(self.open_session_last_edit.keys()):
            first_timestamp = first_timestamp_per_uid.get(uid)
            if first_timestamp is None:
                continue  # untouched this slice - the sweep at the end decides its fate
            if self.open_session_last_edit[uid] + session_death_time < first_timestamp:
                preferences.extend(self._close_session(uid))
            else:
                carried_counts[uid] = self.open_session_counts.pop(uid)
                carried_edit_counts[uid] = self.open_session_edits.pop(uid)
                self.open_session_last_edit.pop(uid)

        previous_timestamp = timestamps.groupby(statement_uid, sort=False).shift()
        # NaT on the first row of a statement compares False, which is what we want: that row
        # either opens a fresh session or continues the carried one.
        starts_new_session = (timestamps - previous_timestamp) > session_death_time

        work = edits.assign(
            seq=starts_new_session.astype(np.int64).groupby(statement_uid, sort=False).cumsum(),
        )

        net_per_reference = (
            work.groupby(["statement_uid", "seq", "reference_key"], sort=False)
            .agg(net_count=("signed", "sum"), domain=("domain", "first"))
            .reset_index()
        )
        session_info = (
            work.groupby(["statement_uid", "seq"], sort=False)
            .agg(edit_count=("signed", "size"), last_timestamp=("timestamp", "max"))
            .reset_index()
        )

        if carried_counts:
            carried_rows = [
                (uid, reference_key, net_count, domain)
                for uid, counts in carried_counts.items()
                for reference_key, (domain, net_count) in counts.items()
            ]
            # Dtypes are pinned rather than inferred: reference_key must stay int64 so the hash
            # survives the concat below intact.
            carried_frame = pd.DataFrame(
                {
                    "statement_uid": pd.Series([row[0] for row in carried_rows], dtype=object),
                    "seq": pd.Series([0] * len(carried_rows), dtype=np.int64),
                    "reference_key": pd.Series([row[1] for row in carried_rows], dtype=np.int64),
                    "net_count": pd.Series([row[2] for row in carried_rows], dtype=np.int64),
                    "domain": pd.Series([row[3] for row in carried_rows], dtype=object),
                }
            )
            net_per_reference = (
                pd.concat([net_per_reference, carried_frame], ignore_index=True)
                .groupby(["statement_uid", "seq", "reference_key"], sort=False)
                .agg(net_count=("net_count", "sum"), domain=("domain", "first"))
                .reset_index()
            )
            carried_mask = (session_info["seq"] == 0) & session_info["statement_uid"].isin(carried_edit_counts)
            session_info.loc[carried_mask, "edit_count"] += (
                session_info.loc[carried_mask, "statement_uid"].map(carried_edit_counts)
            )

        # The newest session of a statement stays open unless the slice ends more than a session
        # length after its last change; every earlier session is concluded by definition.
        last_seq_per_uid = session_info.groupby("statement_uid", sort=False)["seq"].transform("max")
        session_info["closed"] = (session_info["seq"] < last_seq_per_uid) | (
            session_info["last_timestamp"] + session_death_time < slice_end
        )

        closed_info = session_info.loc[session_info["closed"]]
        open_info = session_info.loc[~session_info["closed"]]

        closed_net = net_per_reference.merge(
            closed_info[["statement_uid", "seq"]], on=["statement_uid", "seq"], how="inner"
        )
        if not closed_net.empty:
            session_columns = ["statement_uid", "seq"]
            current = closed_net.loc[closed_net["net_count"] > 0, session_columns + ["domain"]].drop_duplicates()
            replaced = closed_net.loc[closed_net["net_count"] <= 0, session_columns + ["domain"]].drop_duplicates()
            pairs = replaced.merge(current, on=session_columns, suffixes=("_replaced", "_current"))
            pairs = pairs[pairs["domain_replaced"] != pairs["domain_current"]]
            preferences.extend(zip(pairs["domain_replaced"], pairs["domain_current"]))

        self.closed_sessions_last_step += len(closed_info)
        self.closed_sessions_total += len(closed_info)
        closed_edits = int(closed_info["edit_count"].sum()) if not closed_info.empty else 0
        self.session_edits_last_step += closed_edits
        self.session_edits_total += closed_edits
        self.session_references_total += len(closed_net)

        # carry the still open sessions into the next timeslice
        open_net = net_per_reference.merge(
            open_info[["statement_uid", "seq"]], on=["statement_uid", "seq"], how="inner"
        )
        for uid, group in open_net.groupby("statement_uid", sort=False):
            self.open_session_counts[uid] = {
                int(reference_key): (domain, int(net_count))
                for reference_key, domain, net_count in zip(
                    group["reference_key"], group["domain"], group["net_count"]
                )
            }
        for uid, edit_count, last_timestamp in zip(
            open_info["statement_uid"], open_info["edit_count"], open_info["last_timestamp"]
        ):
            self.open_session_edits[uid] = int(edit_count)
            self.open_session_last_edit[uid] = last_timestamp

        # statements carried in from earlier slices that saw no change here
        preferences.extend(self._sweep_expired_sessions(slice_end))
        return preferences

    def calculate_diff(self):
        with self._timed("query"):
            edits = self.datahandler.query_duckdb_df(all_edits_query) # edits with ascending timestamp

        required_columns = {"timestamp", "action", "reference", "entity_id", "property_id", "value_id"}
        if edits is None or edits.empty or not required_columns.issubset(set(edits.columns)):
            # No usable reference edits found in this slice.
            print("Found 0 usable reference edits in this timestep.")
            return []

        with self._timed("derive"):
            # Normalization, domain extraction, hashing and the prefix test are all pure functions of
            # the reference string, so each only has to run once per *distinct* reference instead of
            # once per row. References repeat heavily across a timeslice, and every one of these
            # steps used to allocate a fresh Python string for every row in it.
            codes, uniques = pd.factorize(edits["reference"], sort=False)
            normalized_uniques = normalize_reference_column(pd.Series(uniques))
            allowed_lookup = np.asarray(
                normalized_uniques.str.startswith(ALLOWED_REFERENCE_PREFIXES).fillna(False), dtype=bool
            )
            domain_lookup = np.array(
                [domain_from_reference(reference) for reference in normalized_uniques], dtype=object
            )
            # Sessions count per reference, but only need reference *identity*. Hashing keeps the
            # full URLs out of the session state that is carried across timeslices.
            # Shifted down to 63 bits so the key always fits in int64: a uint64 above 2**63 cannot
            # be held by an int64 column and pandas would silently fall back to float64, which
            # loses precision above 2**53 and would corrupt the key.
            reference_key_lookup = (
                pd.util.hash_pandas_object(normalized_uniques, index=False).to_numpy() >> np.uint64(1)
            ).astype(np.int64)

            if len(codes) and (codes < 0).any():
                # factorize marks missing references with -1. normalize_reference_column maps a
                # missing reference to "", which matches no allowed prefix, so the extra slot these
                # rows point at is one the filter below drops.
                allowed_lookup = np.append(allowed_lookup, False)
                domain_lookup = np.append(domain_lookup, INVALID_URL_DOMAIN)
                reference_key_lookup = np.append(reference_key_lookup, np.int64(0))
                codes = np.where(codes < 0, len(allowed_lookup) - 1, codes)

            keep = allowed_lookup[codes]
            if not keep.any():
                print("Found 0 usable reference edits in this timestep.")
                return []

            codes = codes[keep]
            # reset_index drops the filtered row labels, which would otherwise be carried through the
            # whole session construction as a materialised int64 index.
            edits = edits[keep].reset_index(drop=True)

            # Only the five columns below are read downstream. Building them into a fresh frame keeps
            # the reference URLs and the id columns from being carried into the session construction,
            # rather than assigning onto the wide frame and holding both at once.
            # create statement_uid
            # The statement has multiple ref_property_ids (data, content, subsite etc)
            # -> disregard the other changes for now (i.e. remove dublicate adds) //no group by because we want sequence of adds / deletes
            # NOTE: there may be different ref_property_id s. These contain dates etc
            edits = pd.DataFrame(
                {
                    "statement_uid": (
                        edits["entity_id"].astype(str)
                        + "_"
                        + edits["property_id"].astype(str)
                        + "_"
                        + edits["value_id"].astype(str)
                    ),
                    "timestamp": edits["timestamp"],
                    # The CREATE/DELETE direction is all that is read downstream, so it is resolved
                    # once here instead of comparing strings again per row. int8 is safe because
                    # pandas accumulates integer group sums in int64.
                    "signed": np.where(edits["action"].to_numpy() == "CREATE", 1, -1).astype(np.int8),
                    "reference_key": reference_key_lookup[codes],
                    "domain": domain_lookup[codes],
                }
            )
            del codes, keep, allowed_lookup, domain_lookup, reference_key_lookup, normalized_uniques, uniques

        if self.m_elicitation_method == "session":
            with self._timed("elicit"):
                preferences = self._elicit_preferences_from_sessions(edits)
            print(f"Found {len(preferences)} prefs in this timestep.")
            return preferences

        if self.m_elicitation_method == "replacements":
            replacements : list[tuple] = list()

            # assumes the rows are ordered by timestamp asc.
            for row in edits.itertuples(index=False):
                uid = row.statement_uid
                domain = row.domain
                if row.signed > 0:  # CREATE
                    if uid in self.last_deletes_dict:
                        if self.last_deletes_dict[uid] == domain:
                            # delete followed by create with same reference. Ignore and remove from deletes dict.
                            del self.last_deletes_dict[uid]
                            del self.last_delete_timestamp_dict[uid]
                        else:
                            if self.last_delete_timestamp_dict[uid] + self.time_delta_for_expressing_preference >= row.timestamp:
                                replacements.append((self.last_deletes_dict[uid], domain))
                            del self.last_deletes_dict[uid]
                            del self.last_delete_timestamp_dict[uid]
                    else:
                        self.last_creates_dict[uid] = domain
                        self.last_create_timestamp_dict[uid] = row.timestamp
                else:# row.signed < 0, i.e. DELETE
                    if uid in self.last_creates_dict:
                        if self.last_creates_dict[uid] == domain:
                            # create followed by delete with same reference. Discard create but memorize delete.
                            del self.last_creates_dict[uid]
                            del self.last_create_timestamp_dict[uid]
                        else:
                            if self.last_create_timestamp_dict[uid] + self.time_delta_for_expressing_preference >= row.timestamp:
                                replacements.append((self.last_creates_dict[uid], domain))
                            del self.last_creates_dict[uid]
                            del self.last_create_timestamp_dict[uid]
                    else:
                        self.last_deletes_dict[uid] = domain
                        self.last_delete_timestamp_dict[uid] = row.timestamp

            print(f"Found {len(replacements)} replacements in this timestep.")

            # clean up dicts based on timedelta
            for uid in self.last_delete_timestamp_dict.copy():
                if self.last_delete_timestamp_dict[uid] + self.time_delta_for_expressing_preference < self.datahandler.get_current_end_timestamp():
                    del self.last_deletes_dict[uid]
                    del self.last_delete_timestamp_dict[uid]
            for uid in self.last_create_timestamp_dict.copy():
                if self.last_create_timestamp_dict[uid] + self.time_delta_for_expressing_preference < self.datahandler.get_current_end_timestamp():
                    del self.last_creates_dict[uid]
                    del self.last_create_timestamp_dict[uid]

            return replacements

        raise ValueError(f"Unknown elicitation method: {self.m_elicitation_method}")

    def _classify_with_weight_flow(self, mass_per_domain_dict: dict[str, int]) -> tuple[set[str], set[str], set[str]]:
        """Classify domains by outgoing vs incoming weight flow."""
        dominated_domains = set()
        dominating_domains = set()
        contested_domains = set()

        out_weight_map = self.reference_graph.degree_property_map("out", weight=self.edge_weight).a
        in_weight_map = self.reference_graph.degree_property_map("in", weight=self.edge_weight).a

        # Vertices without any edge carry no preference information at all, so they stay
        # unclassified regardless of their mass.
        for node in np.flatnonzero(out_weight_map + in_weight_map):
            out_weight = float(out_weight_map[node])
            in_weight = float(in_weight_map[node])
            node_domain = self.vertex_domain[node]
            total_weight = out_weight + in_weight

            if total_weight < self.m_weight_flow_min_change_fraction * mass_per_domain_dict.get(node_domain, 0):
                continue

            if out_weight / total_weight >= self.m_weight_flow_threshold:
                dominated_domains.add(node_domain)
            elif in_weight / total_weight >= self.m_weight_flow_threshold:
                dominating_domains.add(node_domain)
            else:
                contested_domains.add(node_domain)

        return dominated_domains, dominating_domains, contested_domains

    def _classify_with_simple_degree(self, pref_graph: GraphView) -> tuple[set[str], set[str], set[str]]:
        """Classify by in/out degree (simple extremes only)."""
        dominated_domains = set()
        dominating_domains = set()
        contested_domains = set()

        out_degree_map = pref_graph.degree_property_map("out").a
        in_degree_map = pref_graph.degree_property_map("in").a

        # Isolated vertices are unclassified, so only the rest need looking at.
        for node in np.flatnonzero(out_degree_map + in_degree_map):
            node_domain = self.vertex_domain[node]
            if out_degree_map[node] == 0:
                dominating_domains.add(node_domain)
            elif in_degree_map[node] == 0:
                dominated_domains.add(node_domain)
            else:
                contested_domains.add(node_domain)

        return dominated_domains, dominating_domains, contested_domains

    def _all_dominators_rebut(self, pref_graph: GraphView, reverse_view: GraphView, component, node) -> bool:
        """Is there a rebutting path back from every domain this node has an outgoing edge to?"""
        dominators = pref_graph.get_out_neighbors(node)
        if dominators.size == 0:
            return False

        # `node -> dominator` is what made it a dominator, so a rebutting path `dominator -> node`
        # would make the two mutually reachable, i.e. members of one strongly connected component.
        # A dominator labelled with a different component therefore cannot rebut at *any* distance
        # and needs no search at all. This only ever skips searches that were going to fail: a
        # coarser labelling would merely skip fewer of them, never answer wrongly.
        if not (component[dominators] == component[node]).all():
            self.rebuttal_skipped_last_step += 1
            return False

        # counted after the trivial exits, so this is the number of searches actually run
        self.rebuttal_searches_last_step += 1

        max_depth = self.max_rebuttal_path_length

        # One sweep on the reversed view gives the distance from every dominator back to `node`,
        # since dist_reversed(node -> y) == dist(y -> node). Naming the dominators as targets is
        # what keeps this affordable: an unweighted search settles a vertex's distance when it
        # discovers it, so graph-tool can stop the moment the last dominator is reached instead of
        # walking the whole depth-limited ball. A dominator that is unreachable (or further away
        # than max_depth) is never discovered, so it keeps the "infinite" sentinel and fails the
        # comparison below, exactly as it did when the distances came from a full sweep.
        distances = shortest_distance(
            reverse_view,
            source=reverse_view.vertex(int(node)),
            target=dominators,
            weights=None,
            max_dist=max_depth,
        )
        return bool((distances <= max_depth).all())

    def _classify_with_simple_with_path(self, pref_graph: GraphView) -> tuple[set[str], set[str], set[str]]:
        """Classify with path rebuttal check for contested domains."""
        dominated_domains = set()
        dominating_domains = set()
        contested_domains = set()

        out_degree_map = pref_graph.degree_property_map("out").a
        in_degree_map = pref_graph.degree_property_map("in").a
        reverse_view = GraphView(pref_graph, reversed=True)

        # Strongly connected components of the preference graph, used to reject hopeless rebuttals
        # without a search. Labelled on the forward view, though the orientation is immaterial:
        # a digraph and its reverse have the same strongly connected components.
        with self._timed("scc"):
            component = label_components(pref_graph, directed=True)[0].a

        # Isolated vertices are unclassified, so only the rest need looking at.
        for node in np.flatnonzero(out_degree_map + in_degree_map):
            node_domain = self.vertex_domain[node]
            if out_degree_map[node] == 0:
                dominating_domains.add(node_domain)
            elif in_degree_map[node] == 0:
                dominated_domains.add(node_domain)
            elif self._all_dominators_rebut(pref_graph, reverse_view, component, node):
                contested_domains.add(node_domain)
            else:
                dominated_domains.add(node_domain)

        return dominated_domains, dominating_domains, contested_domains

    def _mark_accepted_edges(self) -> tuple[int, int]:
        """Mark relevant and clear edges as accepted. Returns (accepted edges, all edges)."""
        # Built as one dense array and assigned in a single go: edge indices are not contiguous
        # after vertices have been recycled, so the array is sized by the index range, not by the
        # number of edges.
        edge_flags = np.zeros(self.reference_graph.edge_index_range, dtype=bool)

        edge_count = self.reference_graph.num_edges()
        self.relevance_weight_cutoff_last_step = 0.0
        if edge_count == 0:
            self.edge_accepted.a = edge_flags
            return 0, 0

        edge_array = self.reference_graph.get_edges(
            eprops=[self.edge_weight, self.reference_graph.edge_index]
        )
        sources = edge_array[:, 0]
        targets = edge_array[:, 1]
        weights = edge_array[:, 2].astype(np.int64)
        edge_indices = edge_array[:, 3].astype(np.int64)

        if self.m_pref_graph_method == "keep_all_edges":
            accepted = np.ones(weights.shape, dtype=bool)
        elif self.m_pref_graph_method == "discard_edge_heuristic":
            cutoff = quantile_weight_cutoff(weights, self.relevance_bottom_edge_fraction)
            relevant = np.ones(weights.shape, dtype=bool) if cutoff is None else weights > cutoff
            self.relevance_weight_cutoff_last_step = float(cutoff) if cutoff is not None else 0.0

            reverse_weights = reverse_edge_weights(
                sources, targets, weights, self.reference_graph.num_vertices()
            )
            clear = weights >= reverse_weights * self.margin
            accepted = relevant & clear
        else:
            raise ValueError(f"Unknown preference graph method: {self.m_pref_graph_method}")

        edge_flags[edge_indices] = accepted
        self.edge_accepted.a = edge_flags
        return int(np.count_nonzero(accepted)), int(edge_count)

    def calculate(self):
        calculate_started = time.perf_counter()
        self.phase_seconds_last_step = {}
        self.rebuttal_searches_last_step = 0
        self.rebuttal_skipped_last_step = 0
        self.recycled_mass_last_step = 0.0
        self.tombstoned_domains_last_step = 0
        self.closed_sessions_last_step = 0
        self.session_edits_last_step = 0

        replacements = self.calculate_diff()

        # get results of counts per domain
        ref_per_domain = self.count_of_references_per_domain.get_last_value()
        if (
            ref_per_domain is None
            or ref_per_domain.empty
            or "domain" not in ref_per_domain.columns
            or "count" not in ref_per_domain.columns
        ):
            print("No reference counts per domain available in this timestep.")
            self.dominated_fraction = 0.0
            self.dominating_fraction = 0.0
            self.contested_fraction = 0.0
            self.unclassified_fraction = 0.0
            self.not_in_graph_fraction = 0.0
            # no edge filtering ran, so do not report the previous timeslice's values
            self.relevance_weight_cutoff_last_step = 0.0
            self.relevant_edge_fraction_last_step = 0.0
            empty_ref_per_domain = pd.DataFrame(columns=["domain", "count"])
            self.write_result(set(), set(), set(), empty_ref_per_domain)
            self._log_phase_timings(time.perf_counter() - calculate_started)
            return

        # we use this for cutting of domains with too little mass
        mass_per_domain_dict = dict(zip(ref_per_domain["domain"], ref_per_domain["count"]))

        total_refs = ref_per_domain["count"].sum()

        #1. MARGIN GRAPH
        # Batch updates to reduce Python <-> C++ boundary calls.
        # Self-preferences are already dropped during elicitation; the guard is belt and braces.
        with self._timed("graph"):
            replacement_edge_deltas = Counter(
                (old_domain, new_domain)
                for old_domain, new_domain in replacements
                if old_domain != new_domain
            )

            for (old_domain, new_domain), delta in replacement_edge_deltas.items():
                old_vertex = self._get_or_add_vertex(old_domain, mass_per_domain_dict)
                new_vertex = self._get_or_add_vertex(new_domain, mass_per_domain_dict)
                self._add_or_increment_edge(old_vertex, new_vertex, delta)

            # Tombstone small domains after updating edge weights for this timestep.
            self._tombstone_low_mass_domains(mass_per_domain_dict)
            self._update_tombstone_states(mass_per_domain_dict)

        #2. PREFERENCE GRAPH - mark edges as "accepted" instead of copying graph
        with self._timed("mark_edges"):
            accepted_edge_count, edge_count = self._mark_accepted_edges()
            self.relevant_edge_fraction_last_step = (
                accepted_edge_count / edge_count if edge_count else 0.0
            )

        pref_graph = GraphView(self.reference_graph, efilt=self.edge_accepted)

        # Invoke the appropriate classification strategy
        with self._timed("classify"):
            if self.m_classification_method == "weight_flow":
                dominated_domains, dominating_domains, contested_domains = self._classify_with_weight_flow(mass_per_domain_dict)
            elif self.m_classification_method == "simple_degree":
                dominated_domains, dominating_domains, contested_domains = self._classify_with_simple_degree(pref_graph)
            elif self.m_classification_method == "simple_with_path":
                dominated_domains, dominating_domains, contested_domains = self._classify_with_simple_with_path(pref_graph)
            else:
                raise ValueError(f"Unknown classification method: {self.m_classification_method}")

        print(f"Found {len(dominated_domains)} clearly dominated domains in this timestep.")
        print(f"Found {len(dominating_domains)} clearly dominating domains in this timestep.")
        print(f"Found {len(contested_domains)} contested domains in this timestep.")

        with self._timed("score"):
            # One pass over the domain frame instead of one isin() scan per class.
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

            # Domains that are in the graph but received no class are "unclassified"; the rest of the
            # mass belongs to domains that never entered the graph at all.
            in_graph_mass = (
                ref_per_domain.loc[ref_per_domain["domain"].isin(self.domain_to_vertex.keys()), "count"].sum()
                if self.domain_to_vertex
                else 0.0
            )
            classified_fraction = self.dominated_fraction + self.dominating_fraction + self.contested_fraction
            in_graph_fraction = float(in_graph_mass) / float(total_refs) if total_refs > 0 else 0.0
            self.unclassified_fraction = max(in_graph_fraction - classified_fraction, 0.0)
            self.not_in_graph_fraction = max(1.0 - in_graph_fraction, 0.0)

        print(f"Fraction of references that are clearly dominated: {self.dominated_fraction:.2%}")
        print(f"Fraction of references that are clearly dominating: {self.dominating_fraction:.2%}")
        print(f"Fraction of references that are contested: {self.contested_fraction:.2%}")
        print(f"Fraction of references in the graph but unclassified: {self.unclassified_fraction:.2%}")
        print(f"Fraction of references whose domain is not in the graph: {self.not_in_graph_fraction:.2%}")

        with self._timed("write"):
            self.write_result(
                dominated_domains,
                dominating_domains,
                contested_domains,
                ref_per_domain,
                accepted_edge_count=accepted_edge_count,
            )

        self._log_phase_timings(time.perf_counter() - calculate_started)

    def write_result(self, dominated, dominating, contested, ref_per_domain: pd.DataFrame, accepted_edge_count: int = 0):
        # this string consists of the main settings used
        settings_string = f"{self.m_classification_method}_{self.m_pref_graph_method}"
        if self.relevance_bottom_edge_fraction is not None:
            bottom_pct = int(self.relevance_bottom_edge_fraction * 100)
            settings_string += f"_dis_{bottom_pct}pct"

        def write(suffix: str, value: float):
            self.datahandler.write_global_metric_value(f"{self.metric_name}_{settings_string}{suffix}", float(value))

        # save our result sets into the result database
        write("_dominated", self.dominated_fraction)
        write("_dominating", self.dominating_fraction)
        write("_contested", self.contested_fraction)
        write("_unclassified", self.unclassified_fraction)
        write("_not_in_graph", self.not_in_graph_fraction)

        # reporting how many references were recycled into reused domains
        write("_recycled_mass_total", self.recycled_mass_cumulative)
        write("_recycled_mass_step", self.recycled_mass_last_step)
        write("_tombstoned_domains_step", self.tombstoned_domains_last_step)

        # which weight the relevance filter settled on, and how much it actually removed
        write("_relevance_weight_cutoff", self.relevance_weight_cutoff_last_step)
        write("_relevant_edge_fraction", self.relevant_edge_fraction_last_step)

        # session statistics
        average_session_size_step = (
            self.session_edits_last_step / self.closed_sessions_last_step
            if self.closed_sessions_last_step
            else 0.0
        )
        average_session_size_total = (
            self.session_edits_total / self.closed_sessions_total if self.closed_sessions_total else 0.0
        )
        average_session_references_total = (
            self.session_references_total / self.closed_sessions_total if self.closed_sessions_total else 0.0
        )
        write("_avg_session_size_step", average_session_size_step)
        write("_avg_session_size_cumulative", average_session_size_total)
        write("_avg_session_references_cumulative", average_session_references_total)
        write("_closed_sessions_step", self.closed_sessions_last_step)
        write("_closed_sessions_total", self.closed_sessions_total)

        # writes the domain names and the result for the top domains
        self.write_top_domain_classification_results(settings_string, dominated, dominating, contested, ref_per_domain)

        # additional fun-fact stats
        number_vertices = self.reference_graph.num_vertices()
        number_edges = self.reference_graph.num_edges()
        edge_density = number_edges / (number_vertices * (number_vertices - 1)) if number_vertices > 1 else 0.0
        average_degree = 2 * number_edges / number_vertices if number_vertices > 0 else 0.0
        write("_graph_edge_density", edge_density)
        write("_graph_average_degree", average_degree)

        # The sparsity of the graph we actually classify on: accepted edges over live vertices.
        live_vertices = self._live_vertex_count()
        tombstoned_vertices = number_vertices - live_vertices
        pref_average_degree = 2 * accepted_edge_count / live_vertices if live_vertices > 0 else 0.0
        pref_edge_density = (
            accepted_edge_count / (live_vertices * (live_vertices - 1)) if live_vertices > 1 else 0.0
        )
        self.pref_graph_max_average_degree = max(self.pref_graph_max_average_degree, pref_average_degree)
        write("_pref_graph_edge_density", pref_edge_density)
        write("_pref_graph_average_degree", pref_average_degree)
        write("_pref_graph_max_average_degree", self.pref_graph_max_average_degree)
        write("_live_vertices", live_vertices)
        write("_tombstoned_vertices", tombstoned_vertices)

        doi_count = self._domain_mass(ref_per_domain, "DOI")
        isbn_count = self._domain_mass(ref_per_domain, "ISBN")

        print("--- Fun Facts ----")
        print(f"Number of domains in the graph is {number_vertices} ({live_vertices} live)")
        print(f"Number of edges in the graph is {number_edges} ({accepted_edge_count} accepted)")
        print(f"edge density of the reference replacement graph: {edge_density:.4f}")
        print(f"Average degree is {average_degree:.2f} (preference graph: {pref_average_degree:.2f})")
        print(f"Relevance weight cutoff: {self.relevance_weight_cutoff_last_step} "
              f"(kept {self.relevant_edge_fraction_last_step:.2%} of edges)")
        print(f"DOI node is in graph" if "DOI" in self.domain_to_vertex else "DOI node is not in graph")
        print(f"ISBN node is in graph" if "ISBN" in self.domain_to_vertex else "ISBN node is not in graph")
        print(f"DOI count: {doi_count}")
        print(f"ISBN count: {isbn_count}")
        print(f"Sessions concluded this step: {self.closed_sessions_last_step} "
              f"(avg size {average_session_size_step:.2f}, cumulative avg {average_session_size_total:.2f})")
        print(f"Recycling:")
        print(f"  Recycled mass this step: {self.recycled_mass_last_step}")
        print(f"  Recycled mass cumulative: {self.recycled_mass_cumulative}")
        print(f"  Tombstoned domains this step: {self.tombstoned_domains_last_step}")

        #locally save a histogram plot of the degree distribution in the graph
        if number_vertices and number_vertices < 10000:
            self._plot_degree_distribution(settings_string, number_vertices)

        # Save graph data for later analysis (graph-tool)
        # self.reference_graph.save(os.path.join(OUTPUT_DIR, f"reference_replacement_graph{...}.gt"))

    def _plot_degree_distribution(self, settings_string: str, number_vertices: int) -> None:
        """Best-effort diagnostic plot of the degree distribution.

        OUTPUT_DIR is gitignored and therefore absent from the rsynced job snapshot, so it has to
        be created here. Nothing about the metric depends on this plot, so a failure to write it
        must never abort a multi-day run.
        """
        try:
            import matplotlib.pyplot as plt
        except Exception as error:
            print(f"Warning: matplotlib unavailable, skipping degree distribution plot ({error}).")
            return

        timestamp = self.datahandler.get_current_end_timestamp()
        output_path = os.path.join(
            OUTPUT_DIR,
            f"degree_distribution_{settings_string}_{timestamp.year}_{timestamp.month}_{timestamp.day}.png",
        )
        try:
            os.makedirs(OUTPUT_DIR, exist_ok=True)
            degrees = self.reference_graph.degree_property_map("total").a[:number_vertices]
            degree_counts = np.bincount(degrees.astype(np.int64))
            print("Plotting histogramm...")
            plt.figure(figsize=(10, 6))
            plt.bar(np.arange(degree_counts.size), degree_counts)
            plt.xlabel("Degree")
            plt.ylabel("Count")
            plt.title("Degree Distribution of Reference Replacement Graph")
            plt.xlim(0, 20) # limit x-axis for better visibility
            plt.yscale("log") # use logarithmic scale for y-axis to better show distribution
            plt.grid(True, which="both", ls="--", lw=0.5)
            plt.tight_layout()
            plt.savefig(output_path)
        except Exception as error:
            print(f"Warning: could not write {output_path} ({error}).")
        finally:
            plt.close("all")

    @staticmethod
    def _domain_mass(ref_per_domain: pd.DataFrame, domain: str) -> float:
        """Mass of a single domain, or 0.0 when it is not in the (tail-truncated) mass table."""
        if ref_per_domain is None or ref_per_domain.empty or "domain" not in ref_per_domain.columns:
            return 0.0
        matching = ref_per_domain.loc[ref_per_domain["domain"] == domain, "count"]
        return float(matching.iloc[0]) if not matching.empty else 0.0

    def write_top_domain_classification_results(
        self,
        settings_string: str,
        dominated: set[str],
        dominating: set[str],
        contested: set[str],
        ref_per_domain: pd.DataFrame,
    ):
        if self.largest_domains_to_write_metric <= 0:
            return

        if len(dominated) == 0 and len(dominating) == 0 and len(contested) == 0:
            return

        # Build a domain->class map with explicit precedence (dominating > dominated > contested).
        class_map = {domain: 0.0 for domain in contested}
        class_map.update({domain: -1.0 for domain in dominated})
        class_map.update({domain: 1.0 for domain in dominating})

        top_domains = (
            ref_per_domain[["domain", "count"]]
            .dropna(subset=["domain"])
            .nlargest(self.largest_domains_to_write_metric, "count")
            .copy()
        )

        if top_domains.empty:
            return

        top_domains["classification_value"] = top_domains["domain"].map(class_map)
        top_domains = top_domains.dropna(subset=["classification_value"])

        if top_domains.empty:
            return

        metric_name = self.metric_name + f"_{settings_string}_domain_classification"
        self.datahandler.write_metric_on_string(metric_name, top_domains, "domain", "classification_value")

    def log_memory(self):
        # Timed separately rather than as a phase of calculate(): main.py calls this *after*
        # calculate() has returned and already emitted its phase line, but *before* it stops the
        # clock, so this is the part of the reported metric runtime that is pure measurement
        # overhead. Deep-sizing the session dicts is the expensive part of it.
        started = time.perf_counter()

        tracked_attrs = {
            "reference_graph": self.reference_graph,
            "domain_to_vertex": self.domain_to_vertex,
            "reusable_vertex_pool": self.reusable_vertex_pool,
            "tombstoned_domains": self.tombstoned_domains,
            "tombstoned_domain_mass": self.tombstoned_domain_mass,
        }

        if self.m_elicitation_method == "replacements":
            tracked_attrs["last_creates_dict_size"] = self.last_creates_dict
            tracked_attrs["last_create_timestamp_dict_size"] = self.last_create_timestamp_dict
            tracked_attrs["last_deletes_dict_size"] = self.last_deletes_dict
            tracked_attrs["last_delete_timestamp_dict_size"] = self.last_delete_timestamp_dict
        if self.m_elicitation_method == "session":
            tracked_attrs["open_session_counts_size"] = self.open_session_counts
            tracked_attrs["open_session_last_edit_size"] = self.open_session_last_edit
            tracked_attrs["open_session_edits_size"] = self.open_session_edits

        log_memory_snapshot(self.metric_name, tracked_attrs)
        log.info(
            "[%s.log_memory] total=%.2fs tracked=%d",
            self.metric_name,
            time.perf_counter() - started,
            len(tracked_attrs),
        )

    def save_cache(self):
        print(
            "Warning: (ReferenceReplacement) save_cache is not implemented: the preference graph, "
            "the open sessions and the tombstone state are lost on restart, so a run cannot be "
            "resumed. Resuming would also need ReferenceCountPerDomain.save_cache."
        )
