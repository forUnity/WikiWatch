from experiment_spec import (
    ExperimentSpec,
    FigurePlotSpec,
    MemoryGroup,
    MemoryPlotSpec,
    MemorySeriesSpec,
    PlotSpec,
    RuntimeGroup,
    RuntimeInput,
    RuntimePlotSpec,
)


LOG_DIR = "../../final_logs"


# ---------------------------------------------------------------------------
# Input files
# ---------------------------------------------------------------------------
SCHEMA_FULLFILLMENT_OPT = (
    f"{LOG_DIR}/additional_logs/schema_fullfillment_optional_2575163.out"
)
SCHEMA_FULLFILLMENT_FORCED = (
    f"{LOG_DIR}/additional_logs/schema_fullfillment_forced_2575162.out"
)

COMPLETENESS_TIMELINESS = (
    f"{LOG_DIR}/additional_logs/completeness_time_2560932.out"
)

CONSISTENCY_SPEED = (
    f"{LOG_DIR}/rust_inbetween_results_1812093.out"
)

CONSISTENCY_MEMORY = (
    f"{LOG_DIR}/prop-constraints-100-mem-350-rust_2550691.out"
)

REFERENCE_100P = (
    f"{LOG_DIR}/reference_completeness_100P_with_2399005.out"
)

REFERENCE_10P = (
    f"{LOG_DIR}/reference_completeness_10P_2398975.out"
)

REFERENCE_10P_WITHOUT = (
    f"{LOG_DIR}/reference_completeness_10P_without_2399000.out"
)

REFERENCE_TRUSTWORTHINESS = (
    f"{LOG_DIR}/additional_logs/reference_trust_flow_final_2559236.out"
)

TIMELINESS_MEMORY = (
    f"{LOG_DIR}/timeliness2-100-mem_2399001.out"
)


# ---------------------------------------------------------------------------
# Experiment
# ---------------------------------------------------------------------------

SPEC = ExperimentSpec(

    # =======================================================================
    # Runtime data
    # =======================================================================

    runtime_groups=(


        RuntimeGroup(
            key="schema_completness",
            label="Schema Completeness",
            inputs=(
                # RuntimeInput(
                #     file=SCHEMA_FULLFILLMENT_OPT,
                #     metrics=(
                #         "schema_fulfillment_optional",
                #     ),
                # ),
                RuntimeInput(
                    file=SCHEMA_FULLFILLMENT_FORCED,
                    metrics=(
                        "schema_fulfillment",
                    ),
                ),
            ),
            exclude_last_timesteps=2,
        ),

        RuntimeGroup(
            key="completeness_currency",
            label="Completeness Currency",
            inputs=(
                RuntimeInput(
                    file=COMPLETENESS_TIMELINESS,
                    metrics=(
                        # "completeness_currency_avg_age_years",
                        # "completeness_currency_avg_score",
                        # "completeness_currency_entity_count",
                        # "completeness_currency_avg_age_years_per_class",
                        # "completeness_currency_avg_score_per_class",
                        # "completeness_currency_entity_count_per_class",
                        "schema_fulfillment_count",
                        "time_entity_complete",
                        "completeness_currency"
                    ),
                ),
            ),
            exclude_last_timesteps=2,
        ),

        RuntimeGroup(
            key="subject_consistency",
            label="Subject Consistency",
            inputs=(
                RuntimeInput(
                    file=CONSISTENCY_SPEED,
                    metrics=(
                        "subject_constraint_violation_count",
                        "statement_count_property_subject",
                        "subject_constraint_violation_total",
                        "subject_constraint_completeness",
                        "subject_constraint_violation_ratio",
                    ),
                    metric_combine="sum",
                ),
            ),
            exclude_last_timesteps=2,
        ),

        RuntimeGroup(
            key="range_consistency",
            label="Range Consistency",
            inputs=(
                RuntimeInput(
                    file=CONSISTENCY_SPEED,
                    metrics=(
                        "range_constraint_violation_count",
                        "range_constraint_violation_count_regular",
                        "range_constraint_violation_count_mandatory",
                        "range_constraint_violation_count_suggestion",
                        "statement_count_property_range_all",
                        "statement_count_property_range_regular",
                        "statement_count_property_range_mandatory",
                        "statement_count_property_range_suggestion",
                        "range_constraint_violation_total_mandatory",
                        "range_constraint_completeness_mandatory",
                        "range_constraint_violation_total_all",
                        "range_constraint_violation_ratio",
                        "range_constraint_completeness_regular",
                        "range_constraint_completeness_suggestion",
                        "range_constraint_violation_total_regular",
                        "range_constraint_violation_ratio_regular",
                        "range_constraint_violation_ratio_suggestion",
                        "range_constraint_violation_ratio_mandatory",
                        "range_constraint_completeness",
                        "range_constraint_violation_total_suggestion",
                    ),
                    metric_combine="sum",
                ),
            ),
            exclude_last_timesteps=2,
        ),

        # RuntimeGroup(
        #     key="reference_completeness_100p",
        #     label="Reference Completeness · 100%",
        #     inputs=(
        #         RuntimeInput(
        #             file=REFERENCE_100P,
        #             metrics=(
        #                 "reference_completeness_df",
        #                 "statement_count",
        #                 "reference_existence_per_statement_count_df",
        #             ),
        #         ),
        #     ),
        #     exclude_last_timesteps=2,
        # ),

        RuntimeGroup(
            key="reference_completeness_10p",
            label="Reference Completeness · 10%",
            inputs=(
                RuntimeInput(
                    file=REFERENCE_10P,
                    metrics=(
                        "reference_completeness_df",
                        "statement_count",
                        "reference_existence_per_statement_count_df",
                    ),
                ),
            ),
            exclude_last_timesteps=2,
        ),

        RuntimeGroup(
            key="reference_trustworthiness",
            label="Reference Trustworthiness",
            inputs=(
                RuntimeInput(
                    file=REFERENCE_TRUSTWORTHINESS,
                    metrics=(
                        "reference_counts_per_domain",
                        "session_flow_counts",
                        "flow_classification_supermajority",
                    ),
                ),
            ),
            exclude_last_timesteps=2,
        ),

        RuntimeGroup(
            key="timeliness",
            label="Data Age at Insertion",
            inputs=(
                RuntimeInput(
                    file=TIMELINESS_MEMORY,
                    metrics=(
                        "data_age_at_insertion",
                    ),
                ),
            ),
            exclude_last_timesteps=2,
        ),
    ),

    # =======================================================================
    # Memory data
    # =======================================================================

    memory_groups=(
        MemoryGroup(
            key="consistency_mem",
            label="Consistency",
            files=(CONSISTENCY_MEMORY,),
            tracked_metrics=None,
        ),

        MemoryGroup(
            key="timeliness_mem",
            label="Timeliness",
            files=(TIMELINESS_MEMORY,),
        ),
        MemoryGroup(
            key="reference_trustworthiness_mem",
            label="Reference Trustworthiness",
            files=(REFERENCE_TRUSTWORTHINESS,),
        ),
        # MemoryGroup(
        #     key="reference_completeness_100p_mem",
        #     label="Reference Completeness · 100%",
        #     files=(REFERENCE_100P,),
        # ),
        MemoryGroup(
            key="reference_completeness_10p_mem",
            label="Reference Completeness · 10%",
            files=(REFERENCE_10P,),
        ),
        MemoryGroup(
            key="schema_completeness_mem",
            label="Schema Completeness",
            files=(
                #SCHEMA_FULLFILLMENT_OPT,
                SCHEMA_FULLFILLMENT_FORCED,
            ),
            combine="sum",
            alignment="strict",
        ),
    ),

    # =======================================================================
    # Figures
    # =======================================================================

    plot=PlotSpec(

        # -------------------------------------------------------------------
        # Runtime figure
        # -------------------------------------------------------------------

        runtime=RuntimePlotSpec(
            unit="minutes", # "seconds" or "hours"
            figsize=(7, 4.5),
            font_scale=1.5,

            title=None,
            x_label=None,
            y_label="Runtime per timestep (minutes)",

            year_tick_interval=1,
            skip_x_ticks=True,
            x_limits=None,

            y_scale="linear", #log
            y_limits=None,

            show_year_lines=True,
            year_line_alpha=0.30,

            show_datapoint_lines=True,
            datapoint_line_alpha=0.07,

            show_grid=True,

            show_legend=True,
            legend_location="upper left",
            #legend_bbox=(0.98, 0.02),
            legend_ncol=1, #2,

            linewidth=1.6,

            output_stem="runtime",
            formats=("pdf", "png"),
        ),

        # -------------------------------------------------------------------
        # Memory figures
        # -------------------------------------------------------------------

        memory_figures=(

            MemoryPlotSpec(
                figsize=(7, 4.5),
                font_scale=1.5,

                #title="Memory usage",
                x_label=None,
                y_label="Memory usage (GiB)",

                year_tick_interval=1,
                skip_x_ticks=True,
                x_limits=None,

                y_scale="linear",
                y_limits=None,

                show_year_lines=True,
                year_line_alpha=0.30,

                show_datapoint_lines=False,
                datapoint_line_alpha=0.07,

                show_grid=True,

                show_legend=True,
                legend_location="upper left",
                legend_bbox=None,
                legend_ncol=1,

                linewidth=1.6,

                output_stem="memory_non_duckdb",
                formats=("pdf", "png"),

                series=(
                    MemorySeriesSpec(
                        value="duckdb_decomposition_gib",
                        mode="underlay",
                        label="Timeslice Changes",
                        groups=(
                            "consistency_mem",
                        ),
                        include_group_in_label=False,
                        fill_alpha=0.20,
                        show_in_legend=True,
                        color="#707070",       # <-- grey color
                    ),

                    MemorySeriesSpec(
                        value="non_duckdb_process_gib",
                        mode="line",
                    ),
                ),
            ),
        ),
    ),
)