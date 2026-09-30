"""Render every plot spec in plot_specs/.

    python plot_all.py

Each spec runs as `python -m paper_plots <spec>` from this folder, so outputs
and .env resolve as usual. A failing spec is reported and the rest still run.
"""
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def main() -> int:
    failed = []
    for spec in sorted((HERE / "plot_specs").glob("*.py")):
        print(f"== {spec.name}", flush=True)
        result = subprocess.run(
            [sys.executable, "-m", "paper_plots", str(spec.relative_to(HERE))],
            cwd=HERE,
        )
        if result.returncode != 0:
            failed.append(spec.name)

    if failed:
        print(f"Failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
