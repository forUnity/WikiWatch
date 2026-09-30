#!/bin/bash
#SBATCH --job-name=compute_metrics
#SBATCH --output=/sc/home/%u/mp2025-wikiwatch/logs/%x_%j.out
#SBATCH --error=/sc/home/%u/mp2025-wikiwatch/logs/%x_%j.err
#SBATCH --constraint="CPU_EXT:AVX-512"   #essential for graph-tool 

#SBATCH --time=72:00:00          # wall time
#SBATCH --account=sci-naumann-mpws2025fn1
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16       # number of CPU cores
#SBATCH --mem=250G                # RAM
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

# use conda environment
conda activate gt-env

if [ "$#" -lt 1 ]; then
  echo "Usage: $0 <variant>"
  exit 1
fi

VARIANT="$1"

# record exact commit that was submitted
echo "Commit at submit time: $(cd "$REPO_DIR" && git rev-parse HEAD)"
echo "Running from snapshot: $WORKDIR"
export GIT_COMMIT_HASH="$(cd "$REPO_DIR" && git rev-parse HEAD)"

python metric_computation_scripts/main.py --log_mem 0 --less_entities 0 --variant "$VARIANT"
