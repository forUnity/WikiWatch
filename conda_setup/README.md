# Conda environment

`environment.yml` defines `gt-env-avx2`, the environment the metric run scripts activate
(`run_metrics_var*.sh`). `graph-tool` is the reason this is a conda environment rather than a
plain venv — it is not reliably pip-installable.

`requirements-pip-only.txt` holds the handful of packages that are not on conda-forge; everything
that *is* on conda-forge belongs in the `dependencies:` list of `environment.yml` instead.

> **There is a second environment.** `run_metrics.sh` activates `gt-env`, not `gt-env-avx2`, and no
> file here defines it. Any dependency added below has to be installed there too, or that script
> fails at import time. `run_metrics_var0.sh`, `run_metrics_var1.sh`, their `_10` variants and
> `run_viz.sh` all use `gt-env-avx2` and are covered by `environment.yml`.

## Updating an existing environment

After a dependency is added to `environment.yml`, update in place rather than recreating — a
rebuild re-solves `graph-tool` and the AVX2 microarch pin, which is slow and can pull a different
`graph-tool` build:

```bash
conda env update -n gt-env-avx2 -f conda_setup/environment.yml --prune
```

`--prune` also removes packages that were dropped from the file, keeping the environment matched
to the spec. Drop it if you have installed extras by hand that you want to keep.

Then verify the environment can actually import what changed:

```bash
conda activate gt-env-avx2
python -c "import tldextract, graph_tool; print(tldextract.__version__)"
```

For a single new package it is also fine to install just that one, which is much faster because it
does not touch the rest of the solve:

```bash
conda install -n gt-env-avx2 -c conda-forge tldextract=5.3.2
```

Do this only as a shortcut — `environment.yml` must still be updated, or the next person to create
the environment from scratch gets a broken run.

## Creating from scratch

```bash
conda env create -f conda_setup/environment.yml
```

## Note on `tldextract`

The reference trustworthiness metrics group web references by registrable domain (domain name plus
public suffix). `tldextract` resolves the public suffix, and the code constructs it with
`suffix_list_urls=()` so it uses the snapshot bundled in the package and never fetches the suffix
list at runtime — compute nodes without outbound network would otherwise stall or fall back to a
different list mid-run.

That makes the pinned version part of the metric definition: upgrading it can change how domains
are grouped and therefore the reported values. Keep `environment.yml` and the top-level
`requirements.txt` on the same version, and treat a bump as a change that invalidates comparison
with earlier runs.
