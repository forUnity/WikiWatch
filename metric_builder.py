from metric import Metric
from metrics.reference_domain_counts import ReferenceCountPerDomain
from metrics.reference_trustworthiness_flow_classification import ReferenceTrustworthinessFlowClassification
from statistics.reference_trustworthiness_preference_graph import ReferenceTrustworthinessPreferenceGraph
from statistics.reference_trustworthiness_session_flow_counts import ReferenceTrustworthinessSessionFlowCounts
from metrics.schema_fulfillment_optional import SchemaFulfillmentOptional
from metrics.schema_fulfillment_per_schema import SchemaFulfillmentPerSchema
from metrics.schema_fulfillment_per_schema_optional import SchemaFulfillmentPerSchemaOptional
from metrics.schema_fulfillment import SchemaFulfillment
from metrics.schema_property_occurrence import SchemaPropertyOccurrence
from statistics.time_entity_complete import TimeEntityComplete
from metrics.completeness_currency import CompletenessCurrency
from metrics.data_age_at_insertion_per_prop import DataAgeAtInsertion
from statistics.entity_count import EntityCount
from statistics.description_count import DescriptionCount
from statistics.label_count import LabelCount
from statistics.property_count import PropertyCount
from statistics.statement_count import StatementCount
from statistics.revision_count import RevisionCount
from statistics.subclass_count import SubclassCount
from metrics.global_division_operator import GlobalDivisionOperator
from metrics.global_sum_operator import GlobalSumOperator
from metrics.has_descript_label_english import HasDescriptlabel
from statistics.reference_count import ReferenceCount
from statistics.reference_existence_per_statement_count_dict import ReferenceExistencePerStatementCountDict
from statistics.reference_existence_per_statement_count_df import ReferenceExistencePerStatementCountDf
from statistics.constraint_violation_range import RangeConstraintViolationCount, RangeConstraintViolationCountMandatory, RangeConstraintViolationCountRegular, RangeConstraintViolationCountSuggestion
# for quicker rust alternative to subject constraint violation count, use statistics.constraint_violation_subject_rust
from statistics.constraint_violation_subject_full import SubjectConstraintViolationCount
from statistics.schema_fulfillment_count import SchemaFulfillmentCount
from statistics.statement_count_range import StatementCountProperty
from statistics.constraint_violation_symmetric_inverse import SymmetricInverseConstraintViolationCount
from statistics.statement_count_property_symm_inv import StatementCountPropertySymmInv
from metrics.global_complement_operator import GlobalComplementOperator
from statistics.statement_count_subject import StatementCountPropertySubject


# ---------------------------------------------------------------------------
# Schema fulfillment
# ---------------------------------------------------------------------------

def build_schema_fulfillment_mandatory_metrics(datahandler, use_cache) -> list[Metric]:
    schema_fulfillment_count = SchemaFulfillmentCount(datahandler, use_cache)
    schema_fulfillment = SchemaFulfillment(datahandler, use_cache, schema_fulfillment_count)
    return [schema_fulfillment_count, schema_fulfillment]


def build_schema_fulfillment_optional_metrics(datahandler, use_cache) -> list[Metric]:
    schema_fulfillment_count = SchemaFulfillmentCount(datahandler, use_cache)
    schema_fulfillment_optional = SchemaFulfillmentOptional(datahandler, use_cache, schema_fulfillment_count)
    return [schema_fulfillment_count, schema_fulfillment_optional]


def build_completeness_currency_metrics(datahandler, use_cache) -> list[Metric]:
    schema_fulfillment_count = SchemaFulfillmentCount(datahandler, use_cache)
    time_entity_complete = TimeEntityComplete(datahandler, use_cache, schema_fulfillment_count)
    completeness_currency = CompletenessCurrency(datahandler, use_cache, time_entity_complete)
    return [schema_fulfillment_count, time_entity_complete, completeness_currency]


def _build_per_schema_metrics(datahandler, use_cache, metric_class) -> list[Metric]:
    """
    One per-schema metric over PER_SCHEMA_ENTITY_SCHEMA_IDS.

    Metrics over all schemas are intentionally left out because the rows are
    restricted to the selected schemas.
    """
    PER_SCHEMA_ENTITY_SCHEMA_IDS: list[int] = [
        10,   # human
        35,   # written work
        112,  # data set
        178,  # algorithm
        270,  # building
        446,  # collection
        481,  # country
    ]

    schema_fulfillment_count = SchemaFulfillmentCount(datahandler, use_cache, PER_SCHEMA_ENTITY_SCHEMA_IDS)
    return [schema_fulfillment_count, metric_class(datahandler, use_cache, schema_fulfillment_count)]


def build_schema_fulfillment_per_schema_mandatory_metrics(datahandler, use_cache) -> list[Metric]:
    """
    Cardinalities as the schemas state them, so a property with a lower
    bound of 0 is fulfilled without a value.
    """
    return _build_per_schema_metrics(datahandler, use_cache, SchemaFulfillmentPerSchema)


def build_schema_fulfillment_per_schema_optional_metrics(datahandler, use_cache) -> list[Metric]:
    """
    Every property is treated as mandatory, so each one needs at least
    one compliant value.
    """
    return _build_per_schema_metrics(datahandler, use_cache, SchemaFulfillmentPerSchemaOptional)


# ---------------------------------------------------------------------------
# Data age at insertion
# ---------------------------------------------------------------------------

def build_data_age_at_insertion_metrics(datahandler, use_cache) -> list[Metric]:
    return [DataAgeAtInsertion(datahandler, use_cache)]


# ---------------------------------------------------------------------------
# Basic statistics
# ---------------------------------------------------------------------------

def build_basic_statistics_metrics(datahandler, use_cache) -> list[Metric]:
    return [
        EntityCount(datahandler, use_cache),
        DescriptionCount(datahandler, use_cache),
        LabelCount(datahandler, use_cache),
        PropertyCount(datahandler, use_cache),
        StatementCount(datahandler, use_cache),
        RevisionCount(datahandler, use_cache),
        SubclassCount(datahandler, use_cache),
        ReferenceCount(datahandler, use_cache),
    ]


# ---------------------------------------------------------------------------
# Average properties / statements
# ---------------------------------------------------------------------------

def build_avg_properties_per_entity_metrics(datahandler, use_cache) -> list[Metric]:
    property_count = PropertyCount(datahandler, use_cache)
    entity_count = EntityCount(datahandler, use_cache)
    avg_properties_per_entity = GlobalDivisionOperator(
        datahandler, "avg_properties_per_entity", property_count, entity_count, use_cache
    )
    return [property_count, entity_count, avg_properties_per_entity]


def build_avg_statements_per_entity_metrics(datahandler, use_cache) -> list[Metric]:
    statement_count = StatementCount(datahandler, use_cache)
    entity_count = EntityCount(datahandler, use_cache)
    avg_statements_per_entity = GlobalDivisionOperator(
        datahandler, "avg_statements_per_entity", statement_count, entity_count, use_cache
    )
    return [statement_count, entity_count, avg_statements_per_entity]


# ---------------------------------------------------------------------------
# Reference completeness
# ---------------------------------------------------------------------------

def build_reference_completeness_df_metrics(datahandler, use_cache) -> list[Metric]:
    reference_existence = ReferenceExistencePerStatementCountDf(datahandler, use_cache)
    statement_count = StatementCount(datahandler, use_cache)
    reference_completeness = GlobalDivisionOperator(
        datahandler, "reference_completeness_df", reference_existence, statement_count, use_cache
    )
    return [reference_existence, statement_count, reference_completeness]


# ---------------------------------------------------------------------------
# Constraint completeness
# ---------------------------------------------------------------------------

def build_subject_constraint_completeness_metrics(datahandler, use_cache) -> list[Metric]:
    subject_constraint_violation_count = SubjectConstraintViolationCount(datahandler, use_cache)
    statement_count_property_subject = StatementCountPropertySubject(
        datahandler,
        use_cache,
        "statement_count_property_subject",
        extra_property_ids=subject_constraint_violation_count.property_ids,
    )
    subject_violation_total = GlobalSumOperator(
        datahandler,
        use_cache,
        "subject_constraint_violation_total",
        subject_constraint_violation_count,
        "violation_count",
    )
    subject_violation_ratio = GlobalDivisionOperator(
        datahandler,
        "subject_constraint_violation_ratio",
        subject_violation_total,
        statement_count_property_subject,
        use_cache,
    )
    subject_completeness = GlobalComplementOperator(
        datahandler, "subject_constraint_completeness", subject_violation_ratio, use_cache
    )
    return [
        subject_constraint_violation_count,
        statement_count_property_subject,
        subject_violation_total,
        subject_violation_ratio,
        subject_completeness,
    ]


def _build_range_constraint_metrics(
    datahandler,
    use_cache,
    violation_class,
    *,
    statement_metric_name: str,
    violation_total_metric_name: str,
    violation_ratio_metric_name: str,
    completeness_metric_name: str,
    constraint_status_filter: str | None = None,
) -> list[Metric]:
    range_violation_count = violation_class(datahandler, use_cache)

    statement_count_kwargs = {}
    if constraint_status_filter is not None:
        statement_count_kwargs["constraint_status_filter"] = constraint_status_filter

    statement_count_property_range = StatementCountProperty(
        datahandler, use_cache, {"Q21510860"}, statement_metric_name, **statement_count_kwargs
    )
    range_violation_total = GlobalSumOperator(
        datahandler, use_cache, violation_total_metric_name, range_violation_count, "violation_count"
    )
    range_violation_ratio = GlobalDivisionOperator(
        datahandler, violation_ratio_metric_name, range_violation_total, statement_count_property_range, use_cache
    )
    range_completeness = GlobalComplementOperator(
        datahandler, completeness_metric_name, range_violation_ratio, use_cache
    )
    return [
        range_violation_count,
        statement_count_property_range,
        range_violation_total,
        range_violation_ratio,
        range_completeness,
    ]


def build_range_constraint_completeness_metrics(datahandler, use_cache) -> list[Metric]:
    return _build_range_constraint_metrics(
        datahandler,
        use_cache,
        RangeConstraintViolationCount,
        statement_metric_name="statement_count_property_range_all",
        violation_total_metric_name="range_constraint_violation_total_all",
        violation_ratio_metric_name="range_constraint_violation_ratio",
        completeness_metric_name="range_constraint_completeness",
    )


def build_range_constraint_completeness_mandatory_metrics(datahandler, use_cache) -> list[Metric]:
    return _build_range_constraint_metrics(
        datahandler,
        use_cache,
        RangeConstraintViolationCountMandatory,
        statement_metric_name="statement_count_property_range_mandatory",
        violation_total_metric_name="range_constraint_violation_total_mandatory",
        violation_ratio_metric_name="range_constraint_violation_ratio_mandatory",
        completeness_metric_name="range_constraint_completeness_mandatory",
        constraint_status_filter="mandatory",
    )


def build_range_constraint_completeness_regular_metrics(datahandler, use_cache) -> list[Metric]:
    return _build_range_constraint_metrics(
        datahandler,
        use_cache,
        RangeConstraintViolationCountRegular,
        statement_metric_name="statement_count_property_range_regular",
        violation_total_metric_name="range_constraint_violation_total_regular",
        violation_ratio_metric_name="range_constraint_violation_ratio_regular",
        completeness_metric_name="range_constraint_completeness_regular",
        constraint_status_filter="regular",
    )


def build_range_constraint_completeness_suggestion_metrics(datahandler, use_cache) -> list[Metric]:
    return _build_range_constraint_metrics(
        datahandler,
        use_cache,
        RangeConstraintViolationCountSuggestion,
        statement_metric_name="statement_count_property_range_suggestion",
        violation_total_metric_name="range_constraint_violation_total_suggestion",
        violation_ratio_metric_name="range_constraint_violation_ratio_suggestion",
        completeness_metric_name="range_constraint_completeness_suggestion",
        constraint_status_filter="suggestion",
    )


# Convenience builder if you want all four range-constraint variants in one run.
def build_all_range_constraint_metrics(datahandler, use_cache) -> list[Metric]:
    return [
        *build_range_constraint_completeness_metrics(datahandler, use_cache),
        *build_range_constraint_completeness_mandatory_metrics(datahandler, use_cache),
        *build_range_constraint_completeness_regular_metrics(datahandler, use_cache),
        *build_range_constraint_completeness_suggestion_metrics(datahandler, use_cache),
    ]

# ---------------------------------------------------------------------------
# Reference trustworthiness
# ---------------------------------------------------------------------------

def build_reference_trustworthiness_majority_metrics(datahandler, use_cache) -> list[Metric]:
        # simply flow.
        reference_domain_count = ReferenceCountPerDomain(datahandler)
        flow_counts =  ReferenceTrustworthinessSessionFlowCounts(datahandler)
        flow_classification = ReferenceTrustworthinessFlowClassification(datahandler, reference_domain_count, flow_counts)

        metrics : list[Metric] = [
            reference_domain_count,
            flow_counts,
            flow_classification,
        ]
        return metrics

def build_reference_trustworthiness_preference_graph_metrics(datahandler, use_cache) -> list[Metric]:
        # only the exported preference graph.
        graph = ReferenceTrustworthinessPreferenceGraph(datahandler)
        metrics : list[Metric] = [
            graph,
        ]
        return metrics


def build_metrics(datahandler, use_cache) -> list[Metric]:
    # seöect metric to build here
    return build_completeness_currency_metrics(datahandler, use_cache)
