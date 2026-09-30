# Preference graph details

Interactive neighbour explorer for the reference preference graphs written by
the trust-graph statistics into `preference_graphs/`.

For a domain `x` it prints the heaviest domains on each side of `x`, ordered by
weight descending, with the reverse edge and the net margin on every row.

## Edge semantics

The snapshots are cumulative directed graphs over reference domains. An edge

```
x -> y      (replaced_domain = x, preferred_domain = y, weight = w)
```

means **`x` was replaced in favour of `y`** `w` times: within a concluded editing
session on one statement, the reference on domain `x` disappeared while the one
on `y` survived. Every (replaced × surviving) pair of a session contributes one
such preference, so `w` accumulates over all sessions up to the snapshot date.

Both directions can exist between the same pair — that is what distinguishes a
one-sided pairing from a contested one, and why each row shows `reverse` and
`net = weight - reverse`.

Consequently:

| direction | table heading | reading |
|---|---|---|
| `x -> y` (outgoing) | `PREFERRED OVER x` | domains that replaced `x` |
| `y -> x` (incoming) | `x PREFERRED OVER` | domains that `x` replaced |

A domain with heavy incoming weight is one the community replaces things *with*;
heavy outgoing weight means it is what gets replaced *away from*.

Roughly half the nodes are bare Wikidata QIDs rather than hostnames, because the
domain extractor passes through references that are not URLs — `Q328` is the
English Wikipedia as a reference target. Displayed QIDs are resolved to labels
against the Wikidata API and cached in `qid_labels.json` (gitignored).

## Usage

```bash
pip install -r preference_graph_details/requirements.txt

# newest snapshot in the directory, then prompt for domains
# (inside the prompt, `:load 2019` switches to the newest snapshot of 2019)
python preference_graph_details/query_preference_graph.py preference_graphs

# one specific snapshot, non-interactive
python preference_graph_details/query_preference_graph.py \
    preference_graphs/preference_graph_2025-08-22.csv.gz --domain ebi.ac.uk
```

The path argument accepts an edge list (`*.csv.gz`), a directory of snapshots
(the newest complete one is loaded — `*.partial.*` files are skipped), or a
`.gt.gz` graph, which resolves to its sibling edge list. The `.gt.gz` files
themselves are not read: they need graph-tool, which is not pip-installable and
is absent from most environments here, while the edge list needs only pandas.

### Domain mass

If `scripts/export_preference_graph_domain_mass.py` (cluster:
`sbatch run_export_domain_mass.sh <date> <run_id>`) has written
`preference_graph_<date>.domain_mass_run<run_id>.tsv.gz` next to the snapshot, the
tables gain two columns: `mass`, the row domain's number of references at the
snapshot date in shorthand (`tail` if it was discarded into the long tail), and
`wt/mass`, the edge weight as a share of the mass of the domain being examined.
With several exported runs the latest (highest run id) is used unless `--run-id`
or `:run` picks another; `:load` stays on the current run where the new snapshot
has it. Without any mass file, a warning is logged and no mass is shown.

### Options

| flag | default | effect |
|---|---|---|
| `--top N` | `50` | rows per direction |
| `--run-id ID` | latest exported | run whose domain mass is shown |
| `--domain X` | – | report `X` and exit instead of prompting; repeatable |
| `--no-fetch-qid-labels` | off | never call Wikidata; cached labels are still used |
| `--label-cache PATH` | `qid_labels.json` here | where labels are cached |
| `--lang` | `en` | label language |
| `--timeout` | `10` | seconds per Wikidata request |
| `--log-level` | `INFO` | `DEBUG` also reports cache hits |

### Interactive commands

```
:top N              rows per direction for later queries
:snapshots          list the snapshots next to the loaded one
:load YEAR|PATH     load another snapshot (YEAR, e.g. 2025: the newest of that year)
:run ID             use the domain mass exported for this run id
:labels on|off      toggle Wikidata label lookups
:help               command list
:quit               leave (Ctrl-C and Ctrl-D also work)
```

Domain lookup is exact — an unknown name reports that it is not in the snapshot.
Since the graphs are cumulative, a domain missing from an early snapshot may well
appear in a later one; `:load` moves between them without restarting.

## Example

```
$ python preference_graph_details/query_preference_graph.py preference_graphs \
      --domain ebi.ac.uk --top 3 --log-level WARNING

==============================================================================
ebi.ac.uk
  mass        253M references (run 2516036; total unknown)
  replaced by 6,585 domain(s), total weight 5,622,039 (2.2% of its mass)
  replaced    290,947 domain(s), total weight 82,732,735 (33% of its mass)
  mass = the row domain's references; wt/mass = weight / mass of ebi.ac.uk; tail = discarded tail
==============================================================================

PREFERRED OVER ebi.ac.uk   (edges ebi.ac.uk -> y: y replaced ebi.ac.uk) -- top 3
  rank  domain                             mass     weight  wt/mass  reverse        net
     1  Q58943792 (InterPro Release 71.0)  1.5M  1,276,666     0.5%        0  1,276,666
     2  Q41725885 (InterPro Release 65.0)    11  1,226,476     0.5%        0  1,226,476
     3  Q32846235 (InterPro Release 64.0)     3  1,108,239     0.4%       11  1,108,228

ebi.ac.uk PREFERRED OVER   (edges y -> ebi.ac.uk: ebi.ac.uk replaced y) -- top 3
  rank  domain                             mass      weight  wt/mass  reverse         net
     1  Q5412157 (Europe PubMed Central)   410M  45,443,657      18%      120  45,443,537
     2  europepmc.org                     57.3M  17,358,273     6.9%      492  17,357,781
     3  Q28018111 (GOA)                    4.0M   4,205,087     1.7%   60,426   4,144,661
```

## Flow classification tuning

`flow_classification_tuning.py` loads a snapshot and its domain mass the same way
(and needs the mass) and replays the evidence guard of
`ReferenceTrustworthinessFlowClassification` (`reference_quality_further_work`
branch): a domain stays unclassified when it has no preferences, or when
`in_weight + out_weight < min(threshold * mass, max_threshold_mass)`. For every
combination of the given parameters it prints how many domains, and how much of
the reference mass, end up classified vs. unclassified.

```bash
python preference_graph_details/flow_classification_tuning.py preference_graphs \
    --threshold 0.01 0.05 0.1 --max-threshold-mass 1000 none
```

`none` disables the `max_threshold_mass` cap; the metric defaults are marked `*`.
`--exclude-no-evidence` only summarises the domains without any evidence (count,
share of domains, share of mass) and makes the table relative to the theoretically
classifiable domains and their mass.

## Notes

* The snapshot is loaded once (~1 s for the 2.35M-edge 2025 file) and factorised
  into integer node codes, so each query is a vectorised scan rather than a
  comparison of millions of strings.
* Label lookups are guarded throughout: batches of 40, an explicit User-Agent, a
  request timeout, a back-off on HTTP 403, and a blanket exception handler that
  logs a warning and leaves the QID bare. An offline machine loses labels, not
  results. Only the QIDs actually displayed are resolved (at most `2 × top + 1`
  per query), and only cache misses are requested.
* A QID the API returns without a label in the requested language is cached as an
  empty string so it is not asked for again; a failed request caches nothing, so
  the next run retries it.
* A QID that does not exist (e.g. a deleted item) makes the API reject its whole
  batch. The resolver drops it, caches it as an empty string, and asks again for
  the rest of the batch.
* The label cache is shared with
  `reference_trustworthiness_details/domain_classification_details.py`.
