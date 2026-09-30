from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

from contextlib import contextmanager
from typing import Callable, Mapping

import logging
import os
import time
import numpy as np
import pandas as pd
import datetime
from graph_tool import Graph

log = logging.getLogger(__name__)

# Where the snapshots go when nothing else is configured. The name is relative on
# purpose: resolve_output_dir turns it into an absolute path under a directory that
# outlives the job.
DEFAULT_OUTPUT_SUBDIR = "preference_graphs"
OUTPUT_DIR_ENV_VAR = "REFERENCE_GRAPH_OUTPUT_DIR"

# Vertex indices are packed two-per-int64 to key the edge set. 32 bits per index
# caps the graph at 2**31 domains, which is several orders of magnitude above
# anything Wikidata produces, and keeps every key non-negative so it sorts.
_VERTEX_INDEX_BITS = np.int64(32)
_VERTEX_INDEX_MASK = np.int64((1 << 32) - 1)

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


def pack_edge_keys(sources, targets) -> np.ndarray:
    """Pack (source, target) vertex index pairs into one int64 key each.

    The key is stable across timesteps, unlike `source * num_vertices + target`, because it does
    not depend on how many vertices the graph currently holds. Both indices must fit in 32 bits.
    """
    sources = np.asarray(sources, dtype=np.int64)
    targets = np.asarray(targets, dtype=np.int64)
    return (sources << _VERTEX_INDEX_BITS) | (targets & _VERTEX_INDEX_MASK)


def match_existing_edges(existing_keys, existing_edge_index, new_keys) -> tuple[np.ndarray, np.ndarray]:
    """Locate `new_keys` among `existing_keys`, returning (found mask, edge index of the found).

    `existing_keys` need not be sorted; it is sorted here and `existing_edge_index` is carried
    along. Relies on the keys being unique, which holds because the graph never gains parallel
    edges: every distinct (source, target) pair is added at most once and incremented afterwards.

    The returned index array is aligned with `new_keys[found]`, i.e. it is already compressed to
    the matched entries.
    """
    new_keys = np.asarray(new_keys, dtype=np.int64)
    existing_keys = np.asarray(existing_keys, dtype=np.int64)
    existing_edge_index = np.asarray(existing_edge_index, dtype=np.int64)

    if existing_keys.size == 0 or new_keys.size == 0:
        return np.zeros(new_keys.size, dtype=bool), np.zeros(0, dtype=np.int64)

    order = np.argsort(existing_keys)
    sorted_keys = existing_keys[order]
    sorted_edge_index = existing_edge_index[order]

    positions = np.searchsorted(sorted_keys, new_keys)
    # searchsorted can return size (past the end); clamp before indexing and let the comparison
    # reject those entries.
    safe_positions = np.minimum(positions, sorted_keys.size - 1)
    found = (positions < sorted_keys.size) & (sorted_keys[safe_positions] == new_keys)

    return found, sorted_edge_index[safe_positions[found]]


def resolve_output_dir(explicit: str | None = None, environ: Mapping[str, str] | None = None) -> str:
    """Absolute directory for the graph snapshots, preferring one that outlives the job.

    Taking a mapping rather than reading os.environ directly keeps this testable. The order is
    explicit argument, then $REFERENCE_GRAPH_OUTPUT_DIR, then $SLURM_SUBMIT_DIR (the shared repo
    the job was submitted from), then the working directory.

    The SLURM preference is the point of this function: run_metrics*.sh rsyncs the repo into a
    job-local $WORKDIR, cd's into it and installs `trap 'rm -rf "$WORKDIR"' EXIT`, so anything
    written to a relative path is deleted the moment the job ends.
    """
    if environ is None:
        environ = os.environ

    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))

    from_env = environ.get(OUTPUT_DIR_ENV_VAR)
    if from_env:
        return os.path.abspath(os.path.expanduser(from_env))

    submit_dir = environ.get("SLURM_SUBMIT_DIR")
    if submit_dir:
        return os.path.abspath(os.path.join(os.path.expanduser(submit_dir), DEFAULT_OUTPUT_SUBDIR))

    return os.path.abspath(DEFAULT_OUTPUT_SUBDIR)


def latest_snapshot(directory: str, suffix: str = ".csv.gz") -> str | None:
    """Newest snapshot in `directory` with the given suffix, or None.

    Not used by this script. Snapshot names end in the slice date (YYYY-MM-DD), so lexicographic
    order is chronological order. Partially written files are named "*.partial.*" and skipped.
    """
    try:
        names = os.listdir(directory)
    except OSError:
        return None

    candidates = sorted(
        name for name in names if name.endswith(suffix) and ".partial." not in name
    )
    if not candidates:
        return None
    return os.path.join(directory, candidates[-1])


def load_preference_graph_edges(path: str) -> pd.DataFrame:
    """Load a saved .csv.gz snapshot as a (replaced_domain, preferred_domain, weight) frame.

    Not used by this script; provided so the qualitative analysis needs nothing but pandas.

        edges = load_preference_graph_edges(latest_snapshot("preference_graphs"))
        # which domains replaced example.com, and how often
        edges[edges.replaced_domain == "example.com"].sort_values("weight", ascending=False)
    """
    return pd.read_csv(path, compression="infer")


def load_preference_graph(path: str):
    """Load a saved .gt.gz snapshot back into a graph_tool Graph.

    Not used by this script. graph_tool is imported lazily so load_preference_graph_edges above
    stays usable in an environment without it.

    The returned graph carries the same internal property maps that were saved:
    `g.vp["domain"]` (string) and `g.ep["weight"]` (int64). `fmt` is left at "auto" so the
    reader derives format and compression from the file name exactly as the writer did.
    """
    from graph_tool import load_graph

    return load_graph(path)


class ReferenceTrustworthinessPreferenceGraph(Metric):
    """Accumulates the weighted preference graph G_pref and saves it for later analysis.

    Sessions of changes to one statement yield pairwise preferences (replaced_domain,
    preferred_domain); each distinct pair is one directed edge whose weight counts how often the
    community expressed it. This class only builds that graph and periodically writes it to disk
    as an artifact for qualitative follow-up ("which domains replaced this dominated domain, and
    how often", "how do references move between domains"). It classifies nothing - that lives in
    the sibling session_flow_counts / flow_classification pair, which is run as a separate job.
    The session elicitation below is therefore duplicated with that script on purpose.

    Nothing here ever reads the graph back, which is why `set_fast_edge_lookup` stays off and the
    per-timestep merge is done with numpy instead of per-pair `Graph.edge()` calls. No vertex or
    edge is ever removed either, so edge indices stay dense - a property the merge relies on.

    Parameters
      session_builder            "vectorized" or "loop"; identical semantics, the loop is the
                                 readable reference implementation
      session_length             idle gap after which a statement's session is concluded
      save_every_n_timesteps     k: write a snapshot every k timesteps (0 or None disables)
      save_at_end                also write a final snapshot from save_cache()
      output_dir                 explicit snapshot directory; see resolve_output_dir
      file_stem                  base name of the snapshot files

    Writes (all prefixed preference_graph):
      _vertices / _edges                     size of the graph
      _new_edges_step                        distinct pairs seen for the first time this step
      _edge_weight_total                     sum of all edge weights
      _edge_weight_added_step                weight added this step
      _preference_pairs_step                 preferences elicited this step (with multiplicity)
      _distinct_pairs_step                   distinct domain pairs among them
      _graph_edge_density / _graph_average_degree
      _graph_max_average_degree              running maximum of the above
      _snapshots_saved_total / _last_save_seconds
    """
    metric_name = "preference_graph"

    def __init__(
        self,
        datahandler : DataHandler,
        session_builder : str = "vectorized",
        session_length: datetime.timedelta = datetime.timedelta(days=2),
        save_every_n_timesteps: int | None = 12,
        save_at_end: bool = True,
        output_dir: str | None = None,
        file_stem: str = "preference_graph",
    ):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []

        # The graph itself. No "accepted" edge property and no tombstone vertex property: edge
        # filtering and relevance heuristics are post-hoc choices for whoever analyses the saved
        # graph, not something to bake into the artifact.
        self.reference_graph = Graph(directed=True)
        self.vertex_domain = self.reference_graph.new_vertex_property("string")
        # "int" is an alias for int32_t; int64_t costs 8 B/edge next to the ~48 B/edge adjacency
        # and removes any need to reason about weights overflowing over a 13 year run.
        self.edge_weight = self.reference_graph.new_edge_property("int64_t")
        self.reference_graph.vp["domain"] = self.vertex_domain
        self.reference_graph.ep["weight"] = self.edge_weight
        # Plain vertex indices, not Vertex descriptors: one fewer Python wrapper object per domain.
        self.domain_to_vertex: dict[str, int] = {}

        self.m_session_death_time = session_length
        if session_builder not in ("vectorized", "loop"):
            raise ValueError("session_builder must be 'vectorized' or 'loop'.")
        self.m_session_builder = session_builder

        # snapshot configuration
        self.save_every_n_timesteps = save_every_n_timesteps
        self.save_at_end = save_at_end
        self.file_stem = file_stem
        self.output_dir = resolve_output_dir(output_dir)
        try:
            os.makedirs(self.output_dir, exist_ok=True)
            log.info("[%s] snapshot directory: %s", self.metric_name, self.output_dir)
        except OSError as error:
            log.warning(
                "[%s] could not create snapshot directory %s (%s); snapshots will be skipped.",
                self.metric_name,
                self.output_dir,
                error,
            )

        # perf accounting: wall time per phase of the timestep.
        self.phase_seconds_last_step: dict[str, float] = {}

        # sessions are per statement. Use statement_uid as key.
        # reference -> (domain, times added minus times removed) for every reference the session touched
        self.open_session_counts: dict[str, dict[int, tuple[str, int]]] = {}
        self.open_session_last_edit: dict[str, datetime.datetime] = {}
        self.open_session_edits: dict[str, int] = {}

        # session accumulators, read by _close_session / the vectorized builder
        self.closed_sessions_last_step = 0
        self.closed_sessions_total = 0
        self.session_edits_last_step = 0
        self.session_edits_total = 0
        self.session_references_total = 0

        # graph accumulators
        self.timesteps_seen = 0
        self.preference_pairs_last_step = 0
        self.distinct_pairs_last_step = 0
        self.new_edges_last_step = 0
        self.edge_weight_added_last_step = 0
        self.edge_weight_total = 0
        self.graph_max_average_degree = 0.0
        self.snapshots_saved_total = 0
        self.last_save_seconds = 0.0
        self.last_saved_timestep = 0

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
            "[%s.calculate] total=%.2fs %s edges=%d new_edges=%d",
            self.metric_name,
            total_seconds,
            phases,
            self.reference_graph.num_edges(),
            self.new_edges_last_step,
        )

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

        with self._timed("elicit"):
            preferences = self._elicit_preferences_from_sessions(edits)
        print(f"Found {len(preferences)} prefs in this timestep.")
        return preferences

    def _vertex_indices_for(self, domains: pd.Series) -> np.ndarray:
        """Vertex index per domain, adding vertices for domains seen for the first time."""
        unseen = [domain for domain in pd.unique(domains) if domain not in self.domain_to_vertex]
        if unseen:
            # add_vertex returns a single Vertex for n == 1 and an iterator otherwise.
            added = self.reference_graph.add_vertex(len(unseen))
            if len(unseen) == 1:
                added = [added]
            for domain, vertex in zip(unseen, added):
                # A string property map has no array representation, so this is per vertex; it
                # only runs for domains that are new, which after the first few timeslices is few.
                self.vertex_domain[vertex] = domain
                self.domain_to_vertex[domain] = int(vertex)

        return domains.map(self.domain_to_vertex).to_numpy(dtype=np.int64)

    def _accumulate_preferences(self, preferences: list[tuple[str, str]]) -> None:
        """Fold this timeslice's preferences into the graph as edge weight.

        One numpy pass rather than a `Graph.edge(s, t)` lookup per pair. Existing weights are
        bumped through the property map's array, which for a scalar value type is a view onto the
        C++ storage; new edges go in through a single `add_edge_list` call, which runs entirely in
        C++ when handed an ndarray.

        Addressing the weight array by edge index is only valid because this class never removes
        an edge, which keeps the indices dense in [0, num_edges).
        """
        self.preference_pairs_last_step = len(preferences)
        self.distinct_pairs_last_step = 0
        self.new_edges_last_step = 0
        self.edge_weight_added_last_step = 0

        if not preferences:
            return

        # Self-preferences are already dropped during elicitation, so every pair is a real edge.
        pair_counts = (
            pd.DataFrame(preferences, columns=["replaced", "preferred"])
            .groupby(["replaced", "preferred"], sort=False)
            .size()
            .reset_index(name="delta")
        )
        self.distinct_pairs_last_step = len(pair_counts)

        sources = self._vertex_indices_for(pair_counts["replaced"])
        targets = self._vertex_indices_for(pair_counts["preferred"])
        deltas = pair_counts["delta"].to_numpy(dtype=np.int64)
        new_keys = pack_edge_keys(sources, targets)

        existing = self.reference_graph.get_edges(eprops=[self.reference_graph.edge_index])
        if existing.shape[0]:
            existing_keys = pack_edge_keys(existing[:, 0], existing[:, 1])
            existing_index = existing[:, 2].astype(np.int64, copy=False)
        else:
            existing_keys = np.zeros(0, dtype=np.int64)
            existing_index = np.zeros(0, dtype=np.int64)

        found, found_edge_index = match_existing_edges(existing_keys, existing_index, new_keys)
        del existing, existing_keys, existing_index

        # Bump before adding: growing the edge set reallocates the array `a` is a view of.
        if found_edge_index.size:
            self.edge_weight.a[found_edge_index] += deltas[found]

        new_edges = ~found
        if new_edges.any():
            self.reference_graph.add_edge_list(
                np.column_stack((sources[new_edges], targets[new_edges], deltas[new_edges])),
                eprops=[self.edge_weight],
            )
            self.new_edges_last_step = int(np.count_nonzero(new_edges))

        self.edge_weight_added_last_step = int(deltas.sum())
        self.edge_weight_total += self.edge_weight_added_last_step

    def _edge_rows(self) -> pd.DataFrame:
        """The graph as a (replaced_domain, preferred_domain, weight) frame."""
        edges = self.reference_graph.get_edges(eprops=[self.edge_weight])
        vertex_count = self.reference_graph.num_vertices()
        # Creation adds exactly one entry per index and nothing is ever removed, so inverting
        # domain_to_vertex gives a complete, dense index -> domain map.
        domain_of_index = np.empty(vertex_count, dtype=object)
        for domain, index in self.domain_to_vertex.items():
            domain_of_index[index] = domain

        if edges.shape[0] == 0:
            return pd.DataFrame(
                {
                    "replaced_domain": pd.Series(dtype=object),
                    "preferred_domain": pd.Series(dtype=object),
                    "weight": pd.Series(dtype=np.int64),
                }
            )

        return pd.DataFrame(
            {
                "replaced_domain": domain_of_index[edges[:, 0].astype(np.int64, copy=False)],
                "preferred_domain": domain_of_index[edges[:, 1].astype(np.int64, copy=False)],
                "weight": edges[:, 2].astype(np.int64, copy=False),
            }
        )

    @staticmethod
    def _write_atomically(base: str, extension: str, write: Callable[[str], None]) -> None:
        """Write `base.extension` through a sibling temporary file, then move it into place.

        A kill mid-write therefore leaves the previous snapshot intact. `.partial` is inserted
        before the extension rather than appended, because graph-tool derives both the format and
        the compression from the file name suffix.
        """
        final_path = f"{base}.{extension}"
        temp_path = f"{base}.partial.{extension}"
        write(temp_path)
        os.replace(temp_path, final_path)

    def _save_snapshot(self) -> None:
        """Write the current graph to the output directory, dated by the end of this timeslice.

        Two formats. `.gt.gz` is the canonical one: "gt" is one of the two graph-tool formats that
        perfectly preserve internal property maps, so `vp["domain"]` and `ep["weight"]` survive the
        round trip along with the topology. `.csv.gz` is the portable edge list, which is what
        answers the qualitative questions with nothing but pandas. `fmt` is left at "auto" on both
        sides so writer and reader derive format and compression from the same file name.

        Nothing about this metric depends on the snapshot, so a failure to write one must never
        abort a multi-day run.
        """
        started = time.perf_counter()
        timestamp = self.datahandler.get_current_end_timestamp()
        base = os.path.join(self.output_dir, f"{self.file_stem}_{timestamp:%Y-%m-%d}")

        try:
            os.makedirs(self.output_dir, exist_ok=True)
            self._write_atomically(base, "gt.gz", self.reference_graph.save)
            edge_rows = self._edge_rows()
            self._write_atomically(
                base,
                "csv.gz",
                lambda path: edge_rows.to_csv(path, index=False, compression="gzip"),
            )
        except Exception as error:
            log.warning("[%s] could not write snapshot %s.* (%s).", self.metric_name, base, error)
            return

        self.snapshots_saved_total += 1
        self.last_saved_timestep = self.timesteps_seen
        self.last_save_seconds = time.perf_counter() - started
        log.info(
            "[%s] saved snapshot %s.{gt.gz,csv.gz} (%d vertices, %d edges) in %.2fs",
            self.metric_name,
            base,
            self.reference_graph.num_vertices(),
            self.reference_graph.num_edges(),
            self.last_save_seconds,
        )

    def calculate(self):
        calculate_started = time.perf_counter()
        self.phase_seconds_last_step = {}
        self.closed_sessions_last_step = 0
        self.session_edits_last_step = 0
        self.timesteps_seen += 1

        preferences = self.calculate_diff()

        with self._timed("graph"):
            self._accumulate_preferences(preferences)
        del preferences

        if self.save_every_n_timesteps and self.timesteps_seen % self.save_every_n_timesteps == 0:
            with self._timed("save"):
                self._save_snapshot()

        with self._timed("write"):
            self.write_result()

        self._log_phase_timings(time.perf_counter() - calculate_started)

    def write_result(self):
        # write_global_metric_value rejects a Python int, so every value goes through float().
        # The suffix carries its own leading underscore.
        def write(suffix: str, value: float):
            self.datahandler.write_global_metric_value(f"{self.metric_name}{suffix}", float(value))

        number_vertices = self.reference_graph.num_vertices()
        number_edges = self.reference_graph.num_edges()
        edge_density = number_edges / (number_vertices * (number_vertices - 1)) if number_vertices > 1 else 0.0
        average_degree = 2 * number_edges / number_vertices if number_vertices > 0 else 0.0
        self.graph_max_average_degree = max(self.graph_max_average_degree, average_degree)

        write("_vertices", number_vertices)
        write("_edges", number_edges)
        write("_new_edges_step", self.new_edges_last_step)
        write("_edge_weight_total", self.edge_weight_total)
        write("_edge_weight_added_step", self.edge_weight_added_last_step)
        write("_preference_pairs_step", self.preference_pairs_last_step)
        write("_distinct_pairs_step", self.distinct_pairs_last_step)
        write("_graph_edge_density", edge_density)
        write("_graph_average_degree", average_degree)
        write("_graph_max_average_degree", self.graph_max_average_degree)
        write("_snapshots_saved_total", self.snapshots_saved_total)
        write("_last_save_seconds", self.last_save_seconds)

        print("--- Preference graph ----")
        print(f"Vertices: {number_vertices}, edges: {number_edges} (+{self.new_edges_last_step} this step)")
        print(f"Total edge weight: {self.edge_weight_total} (+{self.edge_weight_added_last_step} this step)")
        print(f"Average degree: {average_degree:.2f} (max so far {self.graph_max_average_degree:.2f})")
        print(f"Snapshots written so far: {self.snapshots_saved_total}")

    def log_memory(self):
        # Timed separately rather than as a phase of calculate(): main.py calls this *after*
        # calculate() has returned and already emitted its phase line, but *before* it stops the
        # clock, so this is the part of the reported metric runtime that is pure measurement
        # overhead. Deep-sizing the session dicts is the expensive part of it.
        #
        # The graph figure comes from _graph_tool_bytes, whose _ehash constants are uncalibrated
        # to roughly a factor of two. Those two terms do not apply here: fast edge lookup is off,
        # so the estimate is the adjacency plus the property maps and is correspondingly tighter.
        started = time.perf_counter()

        tracked_attrs = {
            "reference_graph": self.reference_graph,
            "domain_to_vertex": self.domain_to_vertex,
            "open_session_counts_size": self.open_session_counts,
            "open_session_last_edit_size": self.open_session_last_edit,
            "open_session_edits_size": self.open_session_edits,
        }

        log_memory_snapshot(self.metric_name, tracked_attrs)
        log.info(
            "[%s.log_memory] total=%.2fs tracked=%d",
            self.metric_name,
            time.perf_counter() - started,
            len(tracked_attrs),
        )

    def save_cache(self):
        """End-of-run hook. Writes the final snapshot; the in-memory state is not resumable."""
        if self.save_at_end and self.last_saved_timestep != self.timesteps_seen:
            self._save_snapshot()
        print(
            "Warning: (ReferenceTrustworthinessPreferenceGraph) save_cache does not persist the "
            "open sessions, so a run cannot be resumed. The graph itself is written to "
            f"{self.output_dir}."
        )
