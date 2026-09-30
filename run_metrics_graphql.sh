#!/bin/bash
#SBATCH --job-name=reference_trust_flow_final
#SBATCH --output=/sc/home/%u/mp2025-wikiwatch/logs/%x_%j.out
#SBATCH --error=/sc/home/%u/mp2025-wikiwatch/logs/%x_%j.err
#SBATCH --constraint="CPU_EXT:AVX2&CPU_SKU:5220S"   #essential for graph-tool 

#SBATCH --time=25:00:00          # wall time
#SBATCH --account=x
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=8       # number of CPU cores
#SBATCH --mem=300G                # RAM
#SBATCH --partition=cpu-batch

# we create a copy of the code, so that we can run in a clean environment and not worry about changes to the code during the run
set -euo pipefail
eval "$(conda shell.bash hook)"

# submit-dir is your shared repo
REPO_DIR="$SLURM_SUBMIT_DIR"
mkdir -p "$REPO_DIR/logs"

# job-local snapshot
TMPBASE="${SLURM_TMPDIR:-/tmp}"
WORKDIR="$TMPBASE/mp2025-wikiwatch-$SLURM_JOB_ID"
trap 'rm -rf "$WORKDIR"' EXIT
mkdir -p "$WORKDIR"

# copy code snapshot
rsync -a --delete \
  --exclude '.git' \
  --exclude 'logs' \
  "$REPO_DIR/" "$WORKDIR/"

cd "$WORKDIR"

#conda env create -f environment.yml
conda activate gt-env-avx2

# record exact commit that was submitted
echo "Commit at submit time: $(cd "$REPO_DIR" && git rev-parse HEAD)"
echo "Running from snapshot: $WORKDIR"
export GIT_COMMIT_HASH="$(cd "$REPO_DIR" && git rev-parse HEAD)"

python metric_computation_scripts/main.py --log_mem 0 --less_entities 0 --variant 0
