# Reference trustworthiness — per-domain details

Companion analysis to `log_parsing/paper_plotter/plot_specs/figure_6.py`, which plots only the
aggregate dominated / dominating / contested fractions. This tool goes one level down and asks
**which concrete domains** those fractions are made of, and it does so for every classifier in the
run at once, so the classifiers can be compared side by side.

Outputs, per classifier:

- a console table of the classifier's **global diagnostic series** (value at the as-of timestep plus
  first / min / max / mean over the run) and `output/classification_stats_<run_id>_<classifier>.csv`
  with the full series in long format.
- `output/largest_domains_<run_id>_<date>_<classifier>.csv` (plus an aligned console table) — the
  largest classified domains at the final timestep, with their end class and how stable that class
  was.
- `figures/domain_classification_history_<run_id>_<classifier>.pdf` / `.svg` — one line per domain
  for the top 3 dominated, top 3 dominating and top 3 contested domains **as classified at the end**,
  showing domain size on a log y axis over time. Each line segment is coloured by that domain's class
  in that timestep, so class changes are visible along the line.

For the supermajority classifier only (see *Per-year top domains* below):

- a console block per year tick and `output/yearly_top_domains_<run_id>_supermajority.csv` — the
  k largest domains of each class (dominated / dominating / contested / unclassified) at every year
  tick, with the class's mass and the share of it those k domains hold.

And once, when more than one classifier was analysed:

- `output/classification_comparison_<run_id>.csv` (plus a console table) — the end class each
  classifier assigns to the same domains, with a pairwise agreement rate and confusion matrix.

## Where the data comes from

The per-domain inputs are string-keyed metrics in `metric_on_string`, keyed by the reference domain:

| what | metric name | short name | id in the paper DB |
|---|---|---|---|
| domain size | `reference_counts_per_domain` | — | 140 |
| classification (replacement graph) | `reference_replacements_simple_with_path_discard_edge_heuristic_dis_5pct_domain_classification` | `replacements` | 159 |
| classification (flow, supermajority) | `flow_classification_supermajority_domain_classification` | `supermajority` | 197 |
| classification (flow, Wilson test) | `flow_classification_wilson_domain_classification` | `wilson` | 219 |
| classification (replacement, flow variant) | `reference_replacements_weight_flow_keep_all_edges_dis_5pct_domain_classification` | — | 158 |

The three short names are accepted by `--class-metric`; any other metric has to be given by its full
name. Metric ids are per-database, so the script resolves them by name; `--list-metrics` prints the
candidates. By default all three known classifiers are analysed, and the ones a run does not have are
reported and skipped rather than treated as an error.

Classification values are the shared encoding written by
`write_top_domain_classification_results`:

```
-1.0 = dominated     0.0 = contested     +1.0 = dominating     2.0 = unclassified
```

The two flow classifiers write `2.0` explicitly; the older replacement rule leaves unclassified
domains out of the metric instead. A domain **absent** from a timestep is therefore either
unclassified or outside the top-1000-by-size cap the metric applies before writing
(`top_largest_domains_to_write = 1000`); the figure draws both as grey, and the comparison table
calls them `absent`. The size metric prunes its `count <= 1` tail every 360 days, so a domain's size
series can have gaps — the figure breaks the line there instead of interpolating across it.

### Global diagnostic series

Each classifier also writes per-timestep scalars into `metric_value_float`, all named with the
classifier's own prefix — the class fractions (`_dominated`, `_dominating`, `_contested`,
`_unclassified`), and whatever else that classifier tracks: `_median_deff`,
`_mass_weighted_mean_p0`, `_exposure_raised_bar_domains` for the Wilson test, session and graph
statistics (`_avg_session_size_step`, `_graph_average_degree`, `_recycled_mass_total`, …) for the
replacement rule. The script does not hardcode that list: it takes the classification metric's name,
strips `_domain_classification`, and reports every `metric_value_float` metric sharing the remaining
prefix. Pass `--no-stats` to skip them.

### Per-year top domains (supermajority only)

For `flow_classification_supermajority` the report adds, for every year tick of the figure's x axis,
the `--yearly-top` (default 5) largest domains of each class, including unclassified. A year tick is
Jan 1 00:00 Europe/Berlin (where `YearLocator` puts it); the timestep closest to it is used, and only
ticks inside the run's range are reported, regardless of `--as-of`.

Per class it prints the class mass, its share of the total, and the share of the class mass the top
k hold; per domain its mass and its share of the class mass. The class mass is

```
class mass = <prefix>_<class> fraction x (reference_counts_per_domain_total_reference_count
                                          - reference_counts_per_domain_tail_count)
```

because the classifier's fractions are fractions of the summed per-domain mass, i.e. everything but
the discarded tail. The four fractions add up to one.

The class metric holds the largest domains by mass regardless of class, so the top k of a class
within it are its true top k. When fewer than k domains of a class are among them, the line is
marked `[only n among the N largest domains]`: the class's next domains are below the cap.

`--detailed` reports the same breakdown at **every** timestep instead of only at the year ticks,
into `output/timestep_top_domains_<run_id>_supermajority.csv`; the timesteps chosen for a year tick
are marked (`<- year tick 2019`, and the `tick` column). It costs one end-state query per timestep
(≤1000 rows each, ~156k rows for 156 timesteps), and a cache written with it holds every timestep.
Asking for `--detailed` from a cache that only holds the year ticks falls back to those with a
warning.

`--just-top` reports only the largest domains by mass **regardless of class**, at the same timesteps
(year ticks, or every timestep with `--detailed`) and with the same count (`--yearly-top`, default
5), and nothing else: no global series, no largest-domains table, no figure, no comparison. Per
domain it shows the mass, its share of the total mass, the cumulative share of ranks 1..n, its class
and label; the heading shows how much the top k hold together. Written to
`output/yearly_largest_overall_<run_id>_supermajority.csv` (`timestep_largest_overall_...` with
`--detailed`). It reads the supermajority classifier's data, which holds the largest domains of
every class, so only that classifier is fetched.

```bash
python domain_classification_details.py --just-top --yearly-top 20
python domain_classification_details.py --just-top --detailed --from-cache cache
```

Domains that are bare Wikidata QIDs (references that are not URLs, e.g. `Q328` = English Wikipedia)
get a `label` column, resolved via the Wikidata API by `LabelResolver` from
`preference_graph_details/query_preference_graph.py`. Labels are cached in that tool's
`qid_labels.json` (gitignored), so both tools share one cache. All domains of the breakdown are
resolved in one lookup, and within one run a QID is sent to the API at most once, including ones
whose request failed. `--offline` skips the API and shows
only labels already in the cache; a failing request never stops the report, it only leaves labels
empty.

This needs a run with the supermajority classifier, such as the default run 2559236 (for a run
without it, e.g. the replacement-only run 2485182, the section is skipped with a note). It costs one
end-state query per year tick (≤1000 rows each) plus the size metric's few global series.
`--yearly-top 0` turns it off. The cache stores all classified domains at each tick, so any k can be
replayed with `--from-cache`; caches written before this section existed are reported and skipped.

### Share of a domain group: `domain_group_share.py`

A small companion script (it imports its DB and formatting helpers from
`domain_classification_details.py`) that sums the mass of a list of domains at the run's last
timestep and prints its share, against all references (including the discarded tail) and against
the per-domain mass that figure 6's fractions refer to.

By default the group is **Wikipedia**: every Wikipedia on Wikidata, i.e. instances of Q10876391
"Wikipedia language edition" or a subclass (the former editions), plus Q52 "Wikipedia" — 369 QIDs,
listed in `wikipedia_editions.csv` (rebuilt from the Wikidata Query Service with
`--refresh-wikipedia`). These are the references that name a Wikipedia as an item, mostly P143
"imported from Wikimedia project". URL references are grouped by host, so Wikipedia URLs are
matched separately as hosts ending in `wikipedia.org` (e.g. `en.wikipedia.org`,
`de.m.wikipedia.org`) and reported as their own group and combined with the QIDs.

```bash
python domain_group_share.py                                   # Wikipedia, run 2559236
python domain_group_share.py --domains Q5412157 europepmc.org  # any other group
python domain_group_share.py --domains-file list.txt --host-suffix ncbi.nlm.nih.gov
```

Output: a per-domain table per group, the totals, and `output/wikipedia_share[_totals]_<run>_<date>.csv`
(`domain_group_share...` for other groups). A listed domain without a row at the last timestep was
never used or has been discarded into the tail, where it is only part of the tail count. Cost: an
index lookup for the last timestep, full-PK probes for the listed domains, and one read of the size
metric at that single timestep for the host match.

### Where the metric implementations live

The classifiers all live on branch `reference_quality_further_work`:
`metric_computation_scripts/statistics/reference_replacements_classification.py` (used for the paper
run 2485182) and `metric_computation_scripts/metrics/reference_trustworthiness_flow_classification*.py`.
`main`'s copy (`metric_computation_scripts/metrics/reference_trustworthiness.py`) is an older
snapshot and its `DataHandler` there has no `write_metric_on_string`. This script only reads the
database, so the checked-out branch does not matter for running it.

## Usage

```bash
python -m pip install -r requirements.txt
cp .env.example .env      # fill DB_USER / DB_PASS / DB_NAME

# confirm the metric names resolve in this database
python domain_classification_details.py --list-metrics

# every known classifier for this run (also saves a cache for offline re-plotting)
python domain_classification_details.py --run-id 2559236 --cache-dir cache

# just the two flow classifiers
python domain_classification_details.py --class-metric supermajority wilson
```

It should finish in seconds per classifier. If it does not, see *Query cost* below.

Useful flags: `--class-metric` (one or more classifiers, short name or full metric name),
`--per-class` (lines per class, default 3), `--top` (CSV rows, default 50), `--yearly-top`
(domains per class and year tick for the supermajority classifier, default 5, 0 = off), `--detailed`
(that breakdown at every timestep), `--just-top` (only the largest domains regardless of class),
`--offline`
(no Wikidata label lookups), `--as-of` (pick an
earlier timestep instead of the last; a classifier that has no such timestep is skipped),
`--no-stats`, `--outdir`, `--timeout` (`statement_timeout`, default 300 s).

`.env` is looked up in this directory first, then in `log_parsing/paper_plotter/`, so an existing
paper-plotter `.env` is picked up automatically. As there, `DB_HOST` is optional: without it the host
is discovered from SLURM via `squeue --name=wikiwatch-db`.

## Working off-cluster

```bash
# once, on the cluster
python domain_classification_details.py --cache-dir cache

# afterwards, anywhere, no database needed
python domain_classification_details.py --from-cache cache
```

The cache is `meta.json` plus one directory per classifier holding four small CSVs (well under 1 MB
in total). It holds history only for the domains that were selected at fetch time, so raising
`--top` beyond the cached value requires a re-fetch — the script warns and clamps rather than silently
truncating. Caches written before the multi-classifier layout are rejected with a message; re-fetch
them.

## Query cost

`metric_on_string` has **only** the composite primary key `(run_id, metric_id, timestamp,
key_string)` and no secondary index. For run 2485182 the size metric holds roughly 415k domains ×
156 timesteps ≈ 40M rows. Because `key_string` is the *last* PK column, the obvious query for "the
history of these 9 domains" —

```sql
WHERE run_id = ? AND metric_id = <size_id> AND key_string = ANY(...)
```

— has to walk that entire index range. The script avoids this everywhere:

- the timestep list and the end-state slice are read from the **class** metric, which is capped at
  1000 rows per timestep;
- each domain size is fetched by joining on the **full four-column PK** (a `LEFT JOIN` for the
  end-state slice, a `VALUES` join chunked at 5000 pairs for the histories), so every lookup is a
  single index probe.

Total rows pulled per classifier for the defaults: 2 metric ids + ~156 timesteps + ≤1000 end-state
rows + ≤18.4k history cells + a few thousand global-series rows — under 30k rows and under 10 MB of
pandas, regardless of run length or domain count. The work scales linearly in the number of
classifiers analysed. `--explain` prints the plans for the end-state query and the first history
chunk of each classifier; there should be no `Seq Scan` on `metric_on_string`.

**If you extend this script:** never `SELECT` from `metric_on_string` for the size metric without
either an equality on a single `timestamp` or a full-PK join.
