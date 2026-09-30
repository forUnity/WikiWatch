from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

from functools import lru_cache
from urllib.parse import urlparse

import numpy as np
import pandas as pd
import datetime
import tldextract

INVALID_URL_DOMAIN = "__INVALID_URL__"

# References that share one of these prefixes are the ones we can map to a domain.
ALLOWED_REFERENCE_PREFIXES = ("Q", "http", "www", "ISBN", "PMID", "DOI")

# Offline extractor: use the public suffix snapshot bundled with the pinned tldextract
# release. Passing an empty suffix_list_urls prevents a network fetch mid-run, which both
# avoids a stall on the cluster and keeps the domain grouping reproducible across reruns.
# Private suffixes stay disabled (the default), so hosting platforms group into a single
# domain: every blogspot blog becomes "blogspot.com" rather than its own domain, which is the
# granularity a trustworthiness judgement about the platform needs.
_TLD_EXTRACT = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)

# tldextract renamed registered_domain to top_domain_under_public_suffix in 5.3; both have the
# same behaviour, so pick whichever the installed version exposes.
_TOP_DOMAIN_ATTRIBUTE = (
    "top_domain_under_public_suffix"
    if hasattr(tldextract.tldextract.ExtractResult, "top_domain_under_public_suffix")
    else "registered_domain"
)

#1. find all REFERENCE deletions and inserts
#2. memorize deletions as last deletion for STATEMENT
#3. check additions against last delete (if not replacement ggf. also add some score?) => replacement information (a, b)
#4. save domain(a) < domain(b) for this metric

# Aggregating in SQL keeps the python side proportional to the number of *distinct*
# references in the timeslice instead of the number of rows.
all_deletes_query="""--sql
select CAST(old_value AS VARCHAR) as reference, count(*) as occurrences from reference_change
WHERE "action" = 'DELETE' and "change_target" = ''
GROUP BY 1
"""

all_creates_query="""--sql
select CAST(new_value AS VARCHAR) as reference, count(*) as occurrences from reference_change
WHERE "action" = 'CREATE' and "change_target" = ''
GROUP BY 1
"""


def _host_from_reference(reference: str) -> str:
    """Extract the lowercased host of a reference, without port or userinfo.

    References are stored without a scheme fairly often (``www.foo.com/bar``). urlparse maps
    those onto ``path`` and leaves ``netloc`` empty, which used to collapse every schemeless
    reference into a single INVALID_URL_DOMAIN bucket. Prefixing "//" makes urlparse treat the
    leading component as an authority instead.
    """
    to_parse = reference if reference.startswith("http") else "//" + reference
    try:
        return urlparse(to_parse).hostname or ""
    except ValueError:
        return ""


@lru_cache(maxsize=1_000_000)
def _registrable_domain(host: str) -> str:
    """Map a host to its registrable domain (domain name plus public suffix).

    tldextract is roughly an order of magnitude slower than urlparse, so it is memoized. The
    cache is keyed on the host rather than on the full reference because hosts are far fewer
    than URLs and repeat heavily across timeslices.
    """
    if not host:
        return INVALID_URL_DOMAIN
    # Empty for IP literals and single-label hosts such as "localhost".
    return getattr(_TLD_EXTRACT(host), _TOP_DOMAIN_ATTRIBUTE) or INVALID_URL_DOMAIN


#for entities (Q...) we could think about visiting its "page" for the domain..
def domain_from_reference(reference: str) -> str:
    if not isinstance(reference, str) or reference == "":
        return INVALID_URL_DOMAIN

    if reference.startswith("http") or reference.startswith("www"):
        return _registrable_domain(_host_from_reference(reference))
    if reference.startswith("ISBN"):
        return "ISBN"
    if reference.startswith("DOI"):
        return "DOI"
    if reference.startswith("PMID"):
        # Identifier references are grouped into a single domain each: assessing the individual
        # record behind the identifier would need lookups we do not have.
        return "PMID"
    return reference


def domains_from_references(references: pd.Series) -> np.ndarray:
    """Vectorized domain_from_reference: one call per *distinct* reference value."""
    codes, uniques = pd.factorize(references, sort=False)
    lookup = np.array([domain_from_reference(reference) for reference in uniques], dtype=object)
    if len(codes) and (codes < 0).any():
        # factorize marks missing values with -1; give them their own slot.
        lookup = np.append(lookup, INVALID_URL_DOMAIN)
        codes = np.where(codes < 0, len(lookup) - 1, codes)
    if len(lookup) == 0:
        return np.empty(len(codes), dtype=object)
    return lookup[codes]


def normalize_reference_column(references: pd.Series) -> pd.Series:
    """Shared normalization so every reference metric derives the same domain vocabulary."""
    normalized = references.fillna("").astype(str).str.strip('"')
    return normalized.str.replace(" ", "%20")  # format spaces correctly


class ReferenceCountPerDomain(Metric):
    """Cumulative mass (insertions minus deletions) per reference domain.

    Writes:
      reference_counts_per_domain                                  -- count per domain
      reference_counts_per_domain_fractions                        -- count / total per domain
      reference_counts_per_domain_total_reference_count            -- total mass incl. discarded tail
      reference_counts_per_domain_tail_count                       -- mass held by the discarded tail
      reference_counts_per_domain_tail_accounts_for_fraction       -- tail share of the total
    """
    metric_name = "reference_counts_per_domain"

    def __init__(self, datahandler : DataHandler, number_of_uses_to_classify_as_relevant : int = 1, time_interval_to_discard_tail : datetime.timedelta = datetime.timedelta(days=360)):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []

        # Source of truth for the cumulative mass: a Series indexed by domain. Kept as a Series
        # so the per-timeslice update is an index-aligned add rather than a full outer merge.
        self.counts = pd.Series(dtype="float64", name="count")
        self.counts.index.name = "domain"
        self.cache = self._materialize_cache()

        self.time_interval_to_discard_tail = time_interval_to_discard_tail
        self.number_of_uses_to_classify_as_relevant = number_of_uses_to_classify_as_relevant
        self.last_tail_discard_time = None

        self.total_count = 0

    def _materialize_cache(self) -> pd.DataFrame:
        cache = self.counts.rename_axis("domain").reset_index()
        cache.columns = ["domain", "count"]
        return cache

    def calculate_diff(self):
        # one row per distinct reference value, with its number of occurrences
        creates = self.datahandler.query_duckdb_df(all_creates_query)
        deletes = self.datahandler.query_duckdb_df(all_deletes_query)

        empty = pd.DataFrame(columns=["domain", "net_creates"])
        if creates is None:
            creates = pd.DataFrame(columns=["reference", "occurrences"])
        if deletes is None:
            deletes = pd.DataFrame(columns=["reference", "occurrences"])

        #filter in the same way as for other ref metrics
        creates_by_domain = self._sum_occurrences_per_domain(creates)
        deletes_by_domain = self._sum_occurrences_per_domain(deletes)

        if creates_by_domain.empty and deletes_by_domain.empty:
            return empty, 0

        net_creates = creates_by_domain.subtract(deletes_by_domain, fill_value=0.0)
        domain_counts = net_creates.rename("net_creates").rename_axis("domain").reset_index()

        return domain_counts, domain_counts["net_creates"].sum()

    def _sum_occurrences_per_domain(self, references_with_counts: pd.DataFrame) -> pd.Series:
        """Normalize, filter and fold a (reference, occurrences) frame down to per-domain sums."""
        empty = pd.Series(dtype="float64")
        empty.index.name = "domain"
        if references_with_counts is None or references_with_counts.empty:
            return empty

        frame = references_with_counts.copy()
        frame["reference"] = normalize_reference_column(frame["reference"])
        frame = frame[frame["reference"].str.startswith(ALLOWED_REFERENCE_PREFIXES)]
        if frame.empty:
            return empty

        # Two raw values can normalize to the same string; the groupby below re-merges them.
        frame["domain"] = domains_from_references(frame["reference"])
        return frame.groupby("domain")["occurrences"].sum().astype("float64")

    def calculate(self):
        domain_counts, total_count_dif = self.calculate_diff()

        if not domain_counts.empty:
            diff = domain_counts.set_index("domain")["net_creates"].astype("float64")
            self.counts = self.counts.add(diff, fill_value=0.0)
            self.counts.name = "count"
            self.counts.index.name = "domain"

        self.total_count = self.total_count + total_count_dif

        #discard long tail
        if self.last_tail_discard_time is None:
            self.last_tail_discard_time = self.datahandler.get_current_start_timestamp()
        if self.datahandler.get_current_end_timestamp() - self.last_tail_discard_time > self.time_interval_to_discard_tail:
            self.counts = self.counts[self.counts > self.number_of_uses_to_classify_as_relevant]
            self.last_tail_discard_time = self.datahandler.get_current_start_timestamp()

        self.cache = self._materialize_cache()

        self.write_result()


    def get_last_value(self) -> pd.DataFrame:
        return self.cache

    def write_result(self):
        self.datahandler.write_metric_on_string(self.metric_name, self.cache, "domain", "count")

        self.datahandler.write_global_metric_value(self.metric_name + "_total_reference_count", float(self.total_count))

        tail_length = self.total_count - self.cache["count"].sum()
        self.datahandler.write_global_metric_value(self.metric_name + "_tail_count", float(tail_length))
        if self.total_count > 0:
            self.datahandler.write_global_metric_value(self.metric_name + "_tail_accounts_for_fraction", float(tail_length) / float(self.total_count))
        else:
            self.datahandler.write_global_metric_value(self.metric_name + "_tail_accounts_for_fraction", 0.0)

        #the tail_frac / tail_length is the average occurence per domain in the tail.

        fractions = self.cache.copy()
        fractions["fraction"] = fractions["count"].astype(float) / float(self.total_count) if self.total_count else 0.0
        self.datahandler.write_metric_on_string(self.metric_name + "_fractions", fractions, "domain", "fraction")

    def log_memory(self):
        tracked_attrs = {
            "cache_rows": self.cache,
            "counts_rows": self.counts,
            "total_count": self.total_count,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def save_cache(self):
        print("Warning: (ReferenceCountPerDomain) save_cache is not implemented. This metric currently only supports in-memory state.")
