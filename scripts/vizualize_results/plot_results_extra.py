"""Plot additional reference quality outputs for a selected run.

This script reads metric tables in Postgres in read-only mode and creates:
1) pie charts of top-k reference domain mass per timestep,
2) dominated/dominating/contested fraction lines over time for each replacement classification setting,
3) optimization error proxy plots (tail fraction + contracted mass),
4) top-k domain usage line plots per classification class and method,
5) a CSV table with top-k domains per classification class and timestep.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


REFERENCE_COUNTS_METRIC = "reference_counts_per_domain"
REFERENCE_FRACTIONS_METRIC = "reference_counts_per_domain_fractions"
TAIL_FRACTION_METRIC = "reference_counts_per_domain_tail_accounts_for_fraction"
TOTAL_REFERENCE_COUNT_METRIC = "reference_counts_per_domain_total_reference_count"

CLASS_VALUE_TO_NAME = {
	-1.0: "dominated",
	0.0: "contested",
	1.0: "dominating",
}

CLASS_VALUE_TO_COLOR = {
	-1.0: "#C0392B",  # dominated
	0.0: "#F2C94C",   # contested
	1.0: "#1E8449",   # dominating
	"unknown": "#7F8C8D",
}

DOMINATED_RED_SHADES = ["#7F0000", "#A50026", "#CB181D", "#EF3B2C", "#FB6A4A", "#FC9272"]
DOMINATING_BLUE_SHADES = ["#08306B", "#08519C", "#2171B5", "#4292C6", "#6BAED6", "#9ECAE1"]
CONTESTED_YELLOW = "#F2C94C"
OTHER_GRAY = "#9EA3A8"


LOGGER = logging.getLogger(__name__)


def _configure_logging(level: str):
	logging.basicConfig(
		level=getattr(logging, level.upper(), logging.INFO),
		format="%(asctime)s %(levelname)s %(message)s",
	)


@contextmanager
def _timed_step(step_name: str):
	start = time.perf_counter()
	LOGGER.info("[START] %s", step_name)
	try:
		yield
	finally:
		elapsed = time.perf_counter() - start
		LOGGER.info("[DONE] %s in %.2fs", step_name, elapsed)


def _build_engine_read_only():
	load_dotenv(".env")

	db_user = os.environ.get("DB_USER")
	db_pass = os.environ.get("DB_PASS")
	db_name = os.environ.get("DB_NAME")
	db_host = os.environ.get("DB_HOST")
	db_port = os.environ.get("DB_PORT")

	missing = [
		name
		for name, value in {
			"DB_USER": db_user,
			"DB_PASS": db_pass,
			"DB_NAME": db_name,
			"DB_HOST": db_host,
			"DB_PORT": db_port,
		}.items()
		if not value
	]
	if missing:
		raise RuntimeError(f"Missing DB env vars in .env: {', '.join(missing)}")

	# Postgres-level read-only safety for this client connection.
	return create_engine(
		f"postgresql+psycopg2://{db_user}:{db_pass}@{db_host}:{db_port}/{db_name}",
		connect_args={"options": "-c default_transaction_read_only=on"},
	)


def _fetch_df(engine, sql: str, params: dict | None = None) -> pd.DataFrame:
	return pd.read_sql_query(text(sql), engine, params=params)


def _resolve_run_id(engine, run_id: int | None) -> int:
	if run_id is not None:
		return run_id
	result = _fetch_df(engine, "SELECT MAX(id) AS run_id FROM runs")
	if result.empty or pd.isna(result.iloc[0]["run_id"]):
		raise RuntimeError("No runs found in 'runs' table.")
	return int(result.iloc[0]["run_id"])


def _load_metric_on_string(engine, run_id: int, max_timestamp_exclusive) -> pd.DataFrame:
	sql = """
	SELECT
		mos.timestamp,
		mos.key_string,
		mos.value,
		m.name AS metric_name
	FROM metric_on_string AS mos
	JOIN metrics AS m
	  ON m.id = mos.metric_id
	WHERE mos.run_id = :run_id
	  AND mos.timestamp < :max_timestamp_exclusive
	  AND (
		m.name = :fractions_metric
		OR m.name LIKE 'reference_replacements_%_domain_classification'
	  )
	ORDER BY mos.timestamp ASC
	"""
	df = _fetch_df(
		engine,
		sql,
		{
			"run_id": run_id,
			"max_timestamp_exclusive": max_timestamp_exclusive,
			"fractions_metric": REFERENCE_FRACTIONS_METRIC,
		},
	)
	if not df.empty:
		df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
	return df


def _load_reference_counts_on_string(engine, run_id: int, max_timestamp_exclusive) -> pd.DataFrame:
	sql = """
	SELECT
		mos.timestamp,
		mos.key_string,
		mos.value,
		m.name AS metric_name
	FROM metric_on_string AS mos
	JOIN metrics AS m
	  ON m.id = mos.metric_id
	WHERE mos.run_id = :run_id
	  AND mos.timestamp < :max_timestamp_exclusive
	  AND m.name = :counts_metric
	ORDER BY mos.timestamp ASC
	"""
	df = _fetch_df(
		engine,
		sql,
		{
			"run_id": run_id,
			"max_timestamp_exclusive": max_timestamp_exclusive,
			"counts_metric": REFERENCE_COUNTS_METRIC,
		},
	)
	if not df.empty:
		df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
	return df


def _load_metric_value_float(engine, run_id: int, max_timestamp_exclusive) -> pd.DataFrame:
	sql = """
	SELECT
		mvf.timestamp,
		mvf.value,
		m.name AS metric_name
	FROM metric_value_float AS mvf
	JOIN metrics AS m
	  ON m.id = mvf.metric_id
	WHERE mvf.run_id = :run_id
	  AND mvf.timestamp < :max_timestamp_exclusive
	  AND (
		m.name = :tail_metric
		OR m.name = :total_count_metric
		OR m.name LIKE 'reference_replacements_%_dominated'
		OR m.name LIKE 'reference_replacements_%_dominating'
		OR m.name LIKE 'reference_replacements_%_contested'
		OR m.name LIKE 'reference_replacements_%_contracted_mass_step'
		OR m.name LIKE 'reference_replacements_%_contracted_mass_total'
	  )
	ORDER BY mvf.timestamp ASC
	"""
	df = _fetch_df(
		engine,
		sql,
		{
			"run_id": run_id,
			"max_timestamp_exclusive": max_timestamp_exclusive,
			"tail_metric": TAIL_FRACTION_METRIC,
			"total_count_metric": TOTAL_REFERENCE_COUNT_METRIC,
		},
	)
	if not df.empty:
		df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
	return df


def _parse_domain_classification_rows(metric_on_string_df: pd.DataFrame) -> pd.DataFrame:
	df = metric_on_string_df[
		metric_on_string_df["metric_name"].str.contains(r"^reference_replacements_.+_domain_classification$", regex=True)
	].copy()
	if df.empty:
		return df
	df["setting"] = df["metric_name"].str.extract(
		r"^reference_replacements_(.+)_domain_classification$",
		expand=False,
	)
	df = df.dropna(subset=["setting"])
	df = df.rename(columns={"key_string": "domain", "value": "class_value"})
	return df[["timestamp", "setting", "domain", "class_value"]]


def _parse_reference_masses(metric_on_string_df: pd.DataFrame) -> tuple[pd.DataFrame, str]:
	fractions = metric_on_string_df[
		metric_on_string_df["metric_name"] == REFERENCE_FRACTIONS_METRIC
	].copy()
	if not fractions.empty:
		fractions = fractions.rename(columns={"key_string": "domain", "value": "mass"})
		return fractions[["timestamp", "domain", "mass"]], "fraction"

	counts = metric_on_string_df[
		metric_on_string_df["metric_name"] == REFERENCE_COUNTS_METRIC
	].copy()
	if counts.empty:
		return pd.DataFrame(columns=["timestamp", "domain", "mass"]), "none"

	counts = counts.rename(columns={"key_string": "domain", "value": "count"})
	total_per_ts = counts.groupby("timestamp", as_index=False)["count"].sum().rename(columns={"count": "total"})
	merged = counts.merge(total_per_ts, on="timestamp", how="left")
	merged["mass"] = merged["count"] / merged["total"].replace(0.0, pd.NA)
	merged["mass"] = merged["mass"].fillna(0.0)
	return merged[["timestamp", "domain", "mass"]], "count_normalized"


def _parse_classification_global_series(metric_value_df: pd.DataFrame) -> pd.DataFrame:
	parsed_cols = metric_value_df["metric_name"].str.extract(
		r"^reference_replacements_(.+)_(dominated|dominating|contested|contracted_mass_step|contracted_mass_total)$",
		expand=True,
	)
	mask = parsed_cols[0].notna()
	if not mask.any():
		return pd.DataFrame()

	parsed = metric_value_df.loc[mask, ["timestamp", "value"]].copy()
	parsed["setting"] = parsed_cols.loc[mask, 0].to_numpy()
	parsed["series_name"] = parsed_cols.loc[mask, 1].to_numpy()

	wide = (
		parsed.pivot_table(
			index=["timestamp", "setting"],
			columns="series_name",
			values="value",
			aggfunc="first",
		)
		.reset_index()
	)

	for col in ["dominated", "dominating", "contested", "contracted_mass_step", "contracted_mass_total"]:
		if col not in wide.columns:
			wide[col] = 0.0
	return wide


def _get_qid_labels(qids: Iterable[str], timeout_seconds: int = 10) -> dict[str, str]:
	qids = sorted({q for q in qids if re.fullmatch(r"Q\d+", str(q or ""))})
	if not qids:
		return {}

	url = "https://www.wikidata.org/w/api.php"
	labels: dict[str, str] = {}
	batch_size = 40

	for i in range(0, len(qids), batch_size):
		batch = qids[i : i + batch_size]
		params = {
			"action": "wbgetentities",
			"props": "labels",
			"ids": "|".join(batch),
			"languages": "en",
			"format": "json",
		}
		try:
			response = requests.get(
				url,
				params=params,
				timeout=timeout_seconds,
				headers={"User-Agent": "mp2025-wikiwatch-plotter/1.0"},
			)
			response.raise_for_status()
			data = response.json()
			for qid, payload in data.get("entities", {}).items():
				label = payload.get("labels", {}).get("en", {}).get("value")
				if label:
					labels[qid] = label
		except Exception as err:
			# Keep script resilient in offline clusters.
			LOGGER.warning("Could not resolve QID labels for batch starting %s: %s", batch[0], err)
			continue

	return labels


def _build_pie_colors(count: int) -> list:
	if count <= 0:
		return []
	palette = list(plt.cm.tab20.colors) + list(plt.cm.tab20b.colors) + list(plt.cm.tab20c.colors)
	if count <= len(palette):
		return palette[:count]
	return [plt.cm.hsv(i / float(count)) for i in range(count)]


def _class_key_from_value(class_value: float | str | None) -> str:
	if class_value is None:
		return "other"
	try:
		v = float(class_value)
	except Exception:
		return "other"
	if abs(v - (-1.0)) < 1e-9:
		return "dominated"
	if abs(v - 1.0) < 1e-9:
		return "dominating"
	if abs(v - 0.0) < 1e-9:
		return "contested"
	return "other"


def _class_shaded_color(class_key: str, index: int):
	if class_key == "dominated":
		return DOMINATED_RED_SHADES[index % len(DOMINATED_RED_SHADES)]
	if class_key == "dominating":
		return DOMINATING_BLUE_SHADES[index % len(DOMINATING_BLUE_SHADES)]
	if class_key == "contested":
		return CONTESTED_YELLOW
	return OTHER_GRAY


def _choose_classification_setting(
	class_df: pd.DataFrame,
	requested_setting: str | None,
) -> str | None:
	available = sorted(class_df["setting"].unique()) if not class_df.empty else []
	if not available:
		return None
	if requested_setting is None:
		return available[0]
	if requested_setting in available:
		return requested_setting
	raise ValueError(
		f"Requested classification setting '{requested_setting}' not found. "
		f"Available: {', '.join(available)}"
	)


def _safe_setting_name(setting: str | None) -> str:
	if setting is None:
		return "unclassified"
	return re.sub(r"[^A-Za-z0-9_\-]", "_", setting)


def _trustworthiness_title_for_setting(setting: str | None) -> str:
	setting_name = (setting or "").lower()
	classification = "Flow Classification" if "flow" in setting_name else "Replacement Classification"
	return f"Reference Trustworthiness ({classification})"


def _collect_topk_qids_for_pies(mass_df: pd.DataFrame, k: int, max_pies: int) -> set[str]:
	if mass_df.empty:
		return set()
	timestamps = sorted(mass_df["timestamp"].unique())
	if max_pies > 0:
		timestamps = timestamps[-max_pies:]
	mass_subset = mass_df[mass_df["timestamp"].isin(timestamps)]
	topk_subset = (
		mass_subset.sort_values(["timestamp", "mass"], ascending=[True, False])
		.groupby("timestamp", sort=False)
		.head(k)
	)
	return {
		str(domain)
		for domain in topk_subset["domain"].tolist()
		if re.fullmatch(r"Q\d+", str(domain))
	}


def _update_qid_label_cache(existing: dict[str, str], qids: Iterable[str]) -> dict[str, str]:
	valid_qids = {q for q in qids if re.fullmatch(r"Q\d+", str(q))}
	missing = sorted(valid_qids - set(existing.keys()))
	if not missing:
		return existing
	existing.update(_get_qid_labels(missing))
	return existing


def _plot_pies(
	mass_df: pd.DataFrame,
	class_df: pd.DataFrame,
	setting_for_colors: str | None,
	out_dir: Path,
	k: int,
	max_pies: int,
	qid_labels: dict[str, str],
):
	if mass_df.empty:
		print("No reference-domain mass rows found; skipping pie charts.")
		return

	with _timed_step("Prepare top-k pie inputs"):
		timestamps = sorted(mass_df["timestamp"].unique())
		if max_pies > 0:
			timestamps = timestamps[-max_pies:]

		mass_subset = mass_df[mass_df["timestamp"].isin(timestamps)].copy()
		topk_subset = (
			mass_subset.sort_values(["timestamp", "mass"], ascending=[True, False])
			.groupby("timestamp", sort=False)
			.head(k)
		)
		topk_groups = {
			ts: grp.copy()
			for ts, grp in topk_subset.groupby("timestamp", sort=False)
		}
		topk_mass_per_ts = topk_subset.groupby("timestamp", sort=False)["mass"].sum().to_dict()

	pie_dir = out_dir
	pie_dir.mkdir(parents=True, exist_ok=True)

	class_lookup: dict[tuple[pd.Timestamp, str], float] = {}
	if setting_for_colors is not None and not class_df.empty:
		class_setting_df = class_df[class_df["setting"] == setting_for_colors]
		class_lookup = {
			(row.timestamp, row.domain): float(row.class_value)
			for row in class_setting_df.itertuples(index=False)
		}

	for ts in timestamps:
		topk = topk_groups.get(ts)
		if topk is None or topk.empty:
			continue
		topk_mass = float(topk_mass_per_ts.get(ts, 0.0))
		other_mass = max(0.0, 1.0 - topk_mass)

		labels = []
		values = []
		colors = _build_pie_colors(len(topk) + 1)
		class_color_counter = {
			"dominated": 0,
			"dominating": 0,
			"contested": 0,
			"other": 0,
		}

		for idx, row in enumerate(topk.itertuples(index=False)):
			domain = str(row.domain)
			if domain in qid_labels:
				labels.append(f"{qid_labels[domain]} ({domain})")
			else:
				labels.append(domain)
			values.append(float(row.mass))
			if setting_for_colors is not None:
				class_value = class_lookup.get((ts, domain))
				class_key = _class_key_from_value(class_value)
				colors[idx] = _class_shaded_color(class_key, class_color_counter[class_key])
				class_color_counter[class_key] += 1

		if other_mass > 1e-12:
			labels.append("Other")
			values.append(other_mass)
			if setting_for_colors is not None and len(colors) > 0:
				colors[len(values) - 1] = OTHER_GRAY

		fig, ax = plt.subplots(figsize=(12, 8))
		pie_kwargs = {
			"labels": labels,
			"autopct": "%1.1f%%",
			"startangle": 90,
			"colors": colors[: len(values)],
		}

		ax.pie(values, **pie_kwargs)
		ts_str = pd.Timestamp(ts).strftime("%Y-%m-%d")
		title = f"Top-{k} reference domain mass at {ts_str}"
		if setting_for_colors is not None:
			title += f" (colored by {setting_for_colors})"
		ax.set_title(title)
		ax.axis("equal")

		output_file = pie_dir / f"reference_domain_mass_top{k}_{pd.Timestamp(ts).strftime('%Y%m%d_%H%M%S')}.png"
		fig.tight_layout()
		fig.savefig(output_file, dpi=160)
		plt.close(fig)


def _domain_display_name(domain: str, qid_labels: dict[str, str]) -> str:
	if domain in qid_labels:
		return f"{qid_labels[domain]} ({domain})"
	return domain


def _plot_topk_domain_mass_bars_per_class(
	mass_df: pd.DataFrame,
	class_df: pd.DataFrame,
	class_global_df: pd.DataFrame,
	out_dir: Path,
	qid_labels: dict[str, str],
	k: int = 5,
	fetch_qid_labels: bool = True,
	timesteps_per_group: int = 6,
	max_bar_labels: int = 3,
):
	if class_df.empty:
		print("No class rows found; skipping per-class top-k mass bar plots.")
		return
	if mass_df.empty:
		print("No mass rows found; skipping per-class top-k mass bar plots.")
		return

	settings_from_global = sorted(class_global_df["setting"].unique()) if not class_global_df.empty else []
	settings_from_classes = sorted(class_df["setting"].unique())
	settings = settings_from_global if settings_from_global else settings_from_classes

	class_order = ["dominated", "dominating", "contested", "unclassified"]
	class_to_color = {
		"dominated": _class_shaded_color("dominated", 1),
		"dominating": _class_shaded_color("dominating", 1),
		"contested": _class_shaded_color("contested", 0),
		"unclassified": _class_shaded_color("other", 0),
	}

	def _palette_for_class(class_name: str, n: int):
		if n <= 0:
			return []
		cmap_name = {
			"dominated": "Reds",
			"dominating": "Blues",
			"contested": "YlOrBr",
			"unclassified": "Greys",
		}.get(class_name, "Greys")
		cmap = plt.get_cmap(cmap_name)
		vals = np.linspace(0.45, 0.9, max(n, 2))
		return [cmap(v) for v in vals[:n]]

	def _short_label(text: str, max_len: int = 26) -> str:
		return text if len(text) <= max_len else text[: max_len - 3] + "..."

	timesteps_per_group = max(1, int(timesteps_per_group))
	max_bar_labels = max(1, int(max_bar_labels))

	for setting in settings:
		setting_class = class_df[class_df["setting"] == setting][["timestamp", "domain", "class_value"]].copy()
		merged = mass_df.merge(setting_class, on=["timestamp", "domain"], how="left")
		merged["class_name"] = merged["class_value"].map(CLASS_VALUE_TO_NAME).fillna("unclassified")
		if merged.empty:
			LOGGER.info("No rows for setting %s; skipping mass bar plot.", setting)
			continue

		all_ts = sorted(merged["timestamp"].unique())
		if not all_ts:
			continue

		group_rows: list[dict] = []
		group_meta: list[dict] = []
		for group_id, start in enumerate(range(0, len(all_ts), timesteps_per_group)):
			group_ts = all_ts[start : start + timesteps_per_group]
			if not group_ts:
				continue
			group_meta.append(
				{
					"group_id": group_id,
					"start": pd.Timestamp(group_ts[0]),
					"end": pd.Timestamp(group_ts[-1]),
					"size": len(group_ts),
				}
			)
			group_subset = merged[merged["timestamp"].isin(group_ts)]
			for class_name in class_order:
				class_subset = group_subset[group_subset["class_name"] == class_name]
				if class_subset.empty:
					continue

				avg_by_domain = (
					class_subset.groupby("domain", as_index=False)["mass"].sum().rename(columns={"mass": "avg_mass"})
				)
				avg_by_domain["avg_mass"] = avg_by_domain["avg_mass"] / float(len(group_ts))
				avg_by_domain = avg_by_domain.sort_values("avg_mass", ascending=False).head(k)

				for row in avg_by_domain.itertuples(index=False):
					group_rows.append(
						{
							"group_id": group_id,
							"class_name": class_name,
							"domain": str(row.domain),
							"avg_mass": float(row.avg_mass),
						}
					)

		if not group_rows:
			LOGGER.info("No top-k grouped rows for setting %s; skipping mass bar plot.", setting)
			continue

		topk_grouped = pd.DataFrame(group_rows)

		if fetch_qid_labels:
			_update_qid_label_cache(qid_labels, topk_grouped["domain"].unique())

		class_domain_color_map: dict[str, dict[str, tuple]] = {}
		for class_name in class_order:
			domains = sorted(topk_grouped[topk_grouped["class_name"] == class_name]["domain"].unique().tolist())
			palette = _palette_for_class(class_name, len(domains))
			class_domain_color_map[class_name] = {
				domain: palette[i % len(palette)] if palette else class_to_color[class_name]
				for i, domain in enumerate(domains)
			}

		group_heights = (
			topk_grouped.groupby(["group_id", "class_name"], as_index=False)["avg_mass"]
			.sum()
		)

		group_ids = sorted(topk_grouped["group_id"].unique())
		if not group_ids:
			continue

		x = np.arange(len(group_ids), dtype=float) * 1.25
		bar_width = 0.24
		fig_width = max(18, min(48, 1.0 * len(group_ids) + 10))
		fig, ax = plt.subplots(figsize=(fig_width, 10))

		for class_idx, class_name in enumerate(class_order):
			offsets = x + (class_idx - 1.5) * bar_width
			for pos_idx, gid in enumerate(group_ids):
				bar_data = topk_grouped[
					(topk_grouped["group_id"] == gid)
					& (topk_grouped["class_name"] == class_name)
				].sort_values("avg_mass", ascending=False)

				if bar_data.empty:
					# Keep visual 4-bar layout even when empty.
					ax.bar(
						offsets[pos_idx],
						0.0,
						width=bar_width,
						color=class_to_color[class_name],
						alpha=0.3,
					)
					continue

				bottom = 0.0
				bar_domains_display: list[str] = []
				for row in bar_data.itertuples(index=False):
					domain = str(row.domain)
					height = float(row.avg_mass)
					ax.bar(
						offsets[pos_idx],
						height,
						width=bar_width,
						bottom=bottom,
						color=class_domain_color_map[class_name].get(domain, class_to_color[class_name]),
						edgecolor="white",
						linewidth=0.4,
					)
					if len(bar_domains_display) < max_bar_labels:
						bar_domains_display.append(_short_label(_domain_display_name(domain, qid_labels)))

					bottom += height

				if bottom > 0.0 and bar_domains_display:
					label_text = "\n".join(bar_domains_display)
					if len(bar_data) > max_bar_labels:
						label_text += "\n..."
					ax.text(
						offsets[pos_idx],
						bottom + max(0.002, bottom * 0.03),
						label_text,
						ha="center",
						va="bottom",
						fontsize=7,
						rotation=90,
					)

		x_labels = []
		meta_map = {m["group_id"]: m for m in group_meta}
		for gid in group_ids:
			meta = meta_map[gid]
			x_labels.append(
				f"{meta['start'].strftime('%Y-%m-%d')}\n{meta['end'].strftime('%Y-%m-%d')}"
			)

		tick_step = max(1, int(np.ceil(len(group_ids) / 12.0)))
		x_tick_labels = [lbl if idx % tick_step == 0 else "" for idx, lbl in enumerate(x_labels)]
		ax.set_xticks(x)
		ax.set_xticklabels(x_tick_labels, rotation=0, ha="center", fontsize=9)
		ax.set_title(_trustworthiness_title_for_setting(setting))
		ax.set_xlabel("Timestep")
		ax.set_ylabel("Mass (sum of top-k domains in class)")
		ax.grid(alpha=0.25)
		ax.margins(y=0.24)
		fig.tight_layout()

		safe_setting = re.sub(r"[^A-Za-z0-9_\-]", "_", setting)
		output_file = out_dir / f"top{k}_domain_mass_per_class_bar_{safe_setting}.png"
		fig.savefig(output_file, dpi=160)
		plt.close(fig)


def _plot_replacement_fractions(class_global_df: pd.DataFrame, out_dir: Path):
	if class_global_df.empty:
		print("No replacement classification global metrics found; skipping replacement fraction plot.")
		return

	for setting, setting_df in class_global_df.groupby("setting"):
		setting_df = setting_df.sort_values("timestamp")
		series_max = float(setting_df[["dominated", "contested", "dominating"]].max().max())
		y_upper = min(1.0, max(0.01, series_max * 1.10))
		fig, ax = plt.subplots(figsize=(12, 6))
		ax.plot(
			setting_df["timestamp"],
			setting_df["dominated"],
			marker="o",
			linewidth=2,
			markersize=3,
			label="dominated",
			color=CLASS_VALUE_TO_COLOR[-1.0],
		)
		ax.plot(
			setting_df["timestamp"],
			setting_df["contested"],
			marker="o",
			linewidth=2,
			markersize=3,
			label="contested",
			color=CLASS_VALUE_TO_COLOR[0.0],
		)
		ax.plot(
			setting_df["timestamp"],
			setting_df["dominating"],
			marker="o",
			linewidth=2,
			markersize=3,
			label="dominating",
			color=CLASS_VALUE_TO_COLOR[1.0],
		)

		ax.set_title(_trustworthiness_title_for_setting(setting))
		ax.set_xlabel("Timestep")
		ax.set_ylabel("Fraction")
		ax.set_ylim(0.0, y_upper)
		ax.xaxis.set_major_locator(mdates.YearLocator(1))
		ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y"))
		ax.tick_params(axis="x", labelrotation=0)
		ax.legend(loc="best")
		ax.grid(alpha=0.25)
		fig.tight_layout()

		safe_setting = re.sub(r"[^A-Za-z0-9_\-]", "_", setting)
		output_file = out_dir / f"reference_replacement_fractions_{safe_setting}.png"
		fig.savefig(output_file, dpi=160)
		plt.close(fig)


def _plot_optimization_error_proxies(
	metric_value_df: pd.DataFrame,
	class_global_df: pd.DataFrame,
	out_dir: Path,
):
	if metric_value_df.empty and class_global_df.empty:
		print("No optimization-proxy metrics found; skipping optimization plot.")
		return

	tail_df = metric_value_df[metric_value_df["metric_name"] == TAIL_FRACTION_METRIC].copy()
	total_count_df = metric_value_df[
		metric_value_df["metric_name"] == TOTAL_REFERENCE_COUNT_METRIC
	][["timestamp", "value"]].rename(columns={"value": "total_reference_count"})

	fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(12, 10), sharex=True)

	if not tail_df.empty:
		tail_df = tail_df.sort_values("timestamp")
		ax1.plot(tail_df["timestamp"], tail_df["value"], color="#8E44AD", marker="o", markersize=3)
		ax1.set_ylabel("Tail fraction")
		ax1.set_title("Reference-domain optimization error proxy")
		ax1.grid(alpha=0.25)
	else:
		ax1.text(0.02, 0.5, "No tail-fraction data found", transform=ax1.transAxes)
		ax1.set_title("Reference-domain optimization error proxy")

	if not class_global_df.empty:
		merged = class_global_df.merge(total_count_df, on="timestamp", how="left")
		merged["contracted_mass_step_normalized"] = merged["contracted_mass_step"] / merged[
			"total_reference_count"
		].replace(0.0, pd.NA)
		merged["contracted_mass_step_normalized"] = merged["contracted_mass_step_normalized"].fillna(
			merged["contracted_mass_step"]
		)

		for setting, setting_df in merged.groupby("setting"):
			setting_df = setting_df.sort_values("timestamp")
			ax2.plot(
				setting_df["timestamp"],
				setting_df["contracted_mass_step_normalized"],
				marker="o",
				linewidth=2,
				markersize=3,
				label=setting,
			)
		ax2.set_ylabel("Contracted mass step (normalized when possible)")
		ax2.grid(alpha=0.25)
		ax2.legend(loc="best")
	else:
		ax2.text(0.02, 0.5, "No contracted-mass series found", transform=ax2.transAxes)

	ax2.set_xlabel("Timestep")
	fig.tight_layout()

	output_file = out_dir / "reference_optimization_error_proxies.png"
	fig.savefig(output_file, dpi=160)
	plt.close(fig)


def parse_args() -> argparse.Namespace:
	parser = argparse.ArgumentParser(description="Plot additional reference-domain metrics for a run.")
	parser.add_argument("--run-id", type=int, default=None, help="Run ID. If omitted, newest run is used.")
	parser.add_argument(
		"--max-year",
		type=int,
		default=2023,
		help="Only include points in time before Jan 1 of this year (exclusive upper bound).",
	)
	parser.add_argument("--k", type=int, default=25, help="Top-k domains for pie charts.")
	parser.add_argument(
		"--k-per-class-usage",
		type=int,
		default=5,
		help="Top-k domains per class for per-timestep mass bar plots.",
	)
	parser.add_argument(
		"--timesteps-per-group",
		type=int,
		default=6,
		help="Number of consecutive timesteps grouped in per-class mass bar plots.",
	)
	parser.add_argument(
		"--max-bar-labels",
		type=int,
		default=3,
		help="Maximum number of domain labels shown above each class bar.",
	)
	parser.add_argument(
		"--max-pies",
		type=int,
		default=20,
		help="Maximum number of most-recent timesteps to render as pies (<=0 means all).",
	)
	parser.add_argument(
		"--output-dir",
		type=str,
		default="vizualize_results/output_extra",
		help="Directory for generated plots and tables.",
	)
	parser.add_argument(
		"--no-color-pies-by-classification",
		action="store_true",
		help="Disable classification-based pie colors.",
	)
	parser.add_argument(
		"--classification-setting",
		type=str,
		default=None,
		help="Setting key for classification series (e.g. simple_with_path_discard_edge_heuristic).",
	)
	parser.add_argument(
		"--no-fetch-qid-labels",
		action="store_true",
		help="Disable QID label resolution from Wikidata API.",
	)
	parser.add_argument(
		"--log-level",
		type=str,
		default="INFO",
		help="Logging level (DEBUG, INFO, WARNING, ERROR).",
	)
	return parser.parse_args()


def main():
	args = parse_args()
	_configure_logging(args.log_level)
	LOGGER.info("Starting extra plotting script")
	max_timestamp_exclusive = pd.Timestamp(year=int(args.max_year), month=1, day=1, tz="UTC").to_pydatetime()
	LOGGER.info("Applying timestamp filter: < %s", max_timestamp_exclusive.isoformat())
	out_dir = Path(args.output_dir)
	out_dir.mkdir(parents=True, exist_ok=True)

	with _timed_step("Create DB engine"):
		engine = _build_engine_read_only()

	with _timed_step("Resolve run id"):
		run_id = _resolve_run_id(engine, args.run_id)

	print(f"Using run_id={run_id}")

	with _timed_step("Load metric_on_string rows"):
		metric_on_string_df = _load_metric_on_string(engine, run_id, max_timestamp_exclusive)
		LOGGER.info("Loaded metric_on_string rows: %d", len(metric_on_string_df))

	with _timed_step("Load metric_value_float rows"):
		metric_value_df = _load_metric_value_float(engine, run_id, max_timestamp_exclusive)
		LOGGER.info("Loaded metric_value_float rows: %d", len(metric_value_df))

	if metric_on_string_df.empty and metric_value_df.empty:
		raise RuntimeError(f"No matching reference metrics found for run_id={run_id}.")

	with _timed_step("Parse masses and classifications"):
		mass_df, mass_unit = _parse_reference_masses(metric_on_string_df)
		class_df = _parse_domain_classification_rows(metric_on_string_df)
		class_global_df = _parse_classification_global_series(metric_value_df)
		LOGGER.info("Mass rows: %d | Class rows: %d | Global class rows: %d", len(mass_df), len(class_df), len(class_global_df))

	counts_df = pd.DataFrame()
	if mass_unit == "none":
		with _timed_step("Fallback load of reference counts"):
			counts_df = _load_reference_counts_on_string(engine, run_id, max_timestamp_exclusive)
			LOGGER.info("Loaded reference count rows: %d", len(counts_df))
			combined = pd.concat([metric_on_string_df, counts_df], ignore_index=True)
			mass_df, mass_unit = _parse_reference_masses(combined)
		if mass_unit == "count_normalized":
			print("Reference fractions metric missing; used count-normalized fallback.")

	fetch_qid_labels = not args.no_fetch_qid_labels
	LOGGER.info("QID label fetch enabled: %s", fetch_qid_labels)
	available_settings = sorted(class_df["setting"].unique()) if not class_df.empty else []
	pie_settings = available_settings if available_settings else [None]
	LOGGER.info("Pie settings: %s", ", ".join([_safe_setting_name(s) for s in pie_settings]))

	qid_labels: dict[str, str] = {}
	if fetch_qid_labels:
		with _timed_step("Fetch shared QID labels for pie domains"):
			qid_labels = _update_qid_label_cache(qid_labels, _collect_topk_qids_for_pies(mass_df, args.k, args.max_pies))
			LOGGER.info("Cached QID labels: %d", len(qid_labels))

	with _timed_step("Generate top-k pie charts"):
		for setting in pie_settings:
			setting_for_colors = None if args.no_color_pies_by_classification else setting
			setting_dir = out_dir / "pies_by_setting" / _safe_setting_name(setting)
			_plot_pies(
				mass_df=mass_df,
				class_df=class_df,
				setting_for_colors=setting_for_colors,
				out_dir=setting_dir,
				k=args.k,
				max_pies=args.max_pies,
				qid_labels=qid_labels,
			)

	with _timed_step("Generate replacement fraction plot"):
		_plot_replacement_fractions(class_global_df, out_dir)

	with _timed_step(
		f"Generate top-{args.k_per_class_usage} per-class mass bar plots (group={args.timesteps_per_group})"
	):
		_plot_topk_domain_mass_bars_per_class(
			mass_df=mass_df,
			class_df=class_df,
			class_global_df=class_global_df,
			out_dir=out_dir,
			qid_labels=qid_labels,
			k=args.k_per_class_usage,
			fetch_qid_labels=fetch_qid_labels,
			timesteps_per_group=args.timesteps_per_group,
			max_bar_labels=args.max_bar_labels,
		)

	with _timed_step("Generate optimization proxy plot"):
		_plot_optimization_error_proxies(metric_value_df, class_global_df, out_dir)

	print(f"Outputs written to: {out_dir}")


if __name__ == "__main__":
	main()