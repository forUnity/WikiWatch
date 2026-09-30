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

class ReferenceTrustworthinessSessionFlowCounts(Metric):
    """Classifies reference domains from the preferences the community expresses when editing references.

    Sessions of changes to one statement yield pairwise preferences, which accumulate into a
    two counts per domain.

      session_builder                  "vectorized" or "loop"; identical semantics, the loop is
                                       the readable reference implementation

      session length                    timedelta; the maximum gap between edits to the same statement.
    """
    metric_name = "session_flow_counts"
    def __init__(
        self,
        datahandler : DataHandler,
        session_builder : str = "vectorized",
        session_length: datetime.timedelta = datetime.timedelta(days=2),
    ):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []

        self.m_session_death_time = session_length
        if session_builder not in ("vectorized", "loop"):
            raise ValueError("session_builder must be 'vectorized' or 'loop'.")
        self.m_session_builder = session_builder

        # perf accounting: wall time per phase of the timestep, plus how many rebuttal searches
        # the path classifier actually ran (the count the phase timing alone cannot explain).
        self.phase_seconds_last_step: dict[str, float] = {}
        self.rebuttal_searches_last_step = 0
        self.rebuttal_skipped_last_step = 0

        # sessions are per statement. Use statement_uid as key.
        # reference -> (domain, times added minus times removed) for every reference the session touched
        self.open_session_counts: dict[str, dict[int, tuple[str, int]]] = {}
        self.open_session_last_edit: dict[str, datetime.datetime] = {}
        self.open_session_edits: dict[str, int] = {}

        # session accumulators, read by _close_session, the vectorized builder and write_result
        self.closed_sessions_last_step = 0
        self.closed_sessions_total = 0
        self.session_edits_last_step = 0
        self.session_edits_total = 0
        self.session_references_total = 0

        # counters of incoming weight and outgoing weight per domain.
        # These are the cache that is read by other metrics.
        self.in_weight_per_domain: Counter[str] = Counter()
        self.out_weight_per_domain: Counter[str] = Counter()

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
            # No usable reference edits found in this slice. Sessions carried in from earlier
            # slices must still be concluded against *this* slice's end, or they are attributed
            # to whichever later slice happens to contain rows.
            print("Found 0 usable reference edits in this timestep.")
            return self._sweep_expired_sessions(self.datahandler.get_current_end_timestamp())

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
                return self._sweep_expired_sessions(self.datahandler.get_current_end_timestamp())

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


    def calculate(self):
        calculate_started = time.perf_counter()
        self.phase_seconds_last_step = {}
        self.closed_sessions_last_step = 0
        self.session_edits_last_step = 0

        replacements = self.calculate_diff()

        with self._timed("update counters"):
            for replaced_domain, preferred_domain in replacements:
                self.out_weight_per_domain[replaced_domain] += 1
                self.in_weight_per_domain[preferred_domain] += 1

        with self._timed("write"):
            self.write_result()

        self._log_phase_timings(time.perf_counter() - calculate_started)

    def get_last_value(self):
        return self.in_weight_per_domain, self.out_weight_per_domain

    def write_result(self):
        def write(suffix: str, value: float):
            self.datahandler.write_global_metric_value(f"{self.metric_name}{suffix}", float(value))

        # session fun facts statistics
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
        pass


    def log_memory(self):
        # no detailed memory logging implemented, as it is currently not used for the paper results.
        pass

    def save_cache(self):
        print(
            "Warning: (ReferenceReplacement) save_cache is not implemented: the preference graph, "
            "the open sessions and the tombstone state are lost on restart, so a run cannot be "
            "resumed. Resuming would also need ReferenceCountPerDomain.save_cache."
        )
