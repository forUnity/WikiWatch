from __future__ import annotations

import argparse
import importlib.util
from pathlib import Path

from .csv_source import load_csv_series
from .db import fetch_series
from .model import FigureSpec
from .render import render_figure


def _load_figure_spec(path: Path) -> FigureSpec:
    module_spec = importlib.util.spec_from_file_location("paper_plot_config", path)
    if module_spec is None or module_spec.loader is None:
        raise RuntimeError(f"Could not load plot spec: {path}")

    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)

    try:
        figure = module.PLOT
    except AttributeError as exc:
        raise RuntimeError(f"{path} must define a variable named PLOT") from exc

    if not isinstance(figure, FigureSpec):
        raise TypeError(f"PLOT in {path} must be a FigureSpec")
    return figure


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render a paper-quality figure from a Python plot specification."
    )
    parser.add_argument("spec", type=Path, help="Path to a plot-spec Python file")
    args = parser.parse_args()

    figure = _load_figure_spec(args.spec)
    db_series = [series for series in figure.series if not series.is_csv]
    csv_series = [series for series in figure.series if series.is_csv]
    data = fetch_series(db_series) if db_series else {}
    data.update(load_csv_series(csv_series))
    render_figure(figure, data)


if __name__ == "__main__":
    main()
