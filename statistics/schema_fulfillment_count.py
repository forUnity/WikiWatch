from datetime import datetime
import json
import pandas as pd
from metric import Metric
from datahandler import DataHandler
from utils.memory_tracker import log_memory_snapshot

# The cardinalities and the value constraint of every schema property. Small (a few thousand rows) and
# the same for every entity and timestep, so it is read once instead of with every edit.
query_constraints = """
SELECT entity_schema_id, property_id, min, max, value_constraint
FROM postgres_db.entity_schemas AS entity_schemas
"""

# Schema property edits of the current timestep, one row per entity schema of the entity that contains
# the property. DISTINCT ON the value_change primary key and the schema keeps an edit from being counted
# twice for the same row when the entity reaches a schema through several entity_type rows; the remaining
# columns are determined by those, as (entity_schema_id, property_id) is unique in entity_schemas.
query = """
SELECT DISTINCT ON (value_change.revision_id, value_change.value_id, value_change.change_target, entity_schemas.entity_schema_id)
    value_change.entity_id, value_change.property_id, value_change.action, value_change.old_value, value_change.new_value, entity_schemas.entity_schema_id
FROM value_change, postgres_db.entity_type AS entity_type, postgres_db.entity_schemas AS entity_schemas, postgres_db.schema_class_mapping AS schema_class_mapping
WHERE value_change.entity_id = entity_type.entity_id
AND entity_schemas.entity_schema_id = schema_class_mapping.entity_schema_id
AND entity_type.class_id = schema_class_mapping.class_id
AND value_change.property_id = entity_schemas.property_id
{schema_filter}
"""

# One row per schema property of each entity created in the current timestep. The parser marks the
# first change of every entity with action CREATE and target ENTITY, in value_change or sitelink_change.
query_created = """
SELECT created.entity_id, entity_schemas.property_id, entity_schemas.entity_schema_id, COALESCE(entity_schemas.min, 0) <= 0 AS is_within_min_max
FROM (
    SELECT entity_id FROM value_change WHERE target = 'ENTITY' AND action = 'CREATE'
    UNION
    SELECT entity_id FROM sitelink_change WHERE target = 'ENTITY' AND action = 'CREATE'
) AS created, postgres_db.entity_type AS entity_type, postgres_db.entity_schemas AS entity_schemas, postgres_db.schema_class_mapping AS schema_class_mapping
WHERE created.entity_id = entity_type.entity_id
AND entity_schemas.entity_schema_id = schema_class_mapping.entity_schema_id
AND entity_type.class_id = schema_class_mapping.class_id
{schema_filter}
"""

# Cleared once it holds this many values, so that memoizing compliance stays bounded in memory
COMPLIANCE_CACHE_LIMIT = 5_000_000

class SchemaFulfillmentCount(Metric):
    """
    Stores the number of compliant and non-compliant property values for each property for each entity,
    as of the end of the current timestep.
    The final entity schemas are the gold standard: the schemas of an entity are determined by its classes
    in the final entity_type table, and every property of these schemas gets a row, whether it was ever
    edited or not. The rows of an entity are added in the timestep in which it is created, so entities
    that do not exist yet have no rows.
    With entity_schema_ids, only the rows of those entity schemas are built and updated, which is much
    faster but leaves the properties of the other schemas of an entity out of its rows. Metrics over all
    schemas (such as schema_fulfillment) therefore need an unrestricted instance.
    """
    metric_name = "schema_fulfillment_count"
    DICT_TYPE = dict[tuple[int, int, int], tuple[int | None, int | None, bool | None]]
    # (entity_schema_id, property_id) -> (min, max, parsed value constraint)
    CONSTRAINTS_TYPE = dict[tuple[int, int], tuple[int | None, int | None, dict | str | None]]

    def __init__(self, datahandler: DataHandler, use_cache: bool, entity_schema_ids: list[int] | None = None):
        self.datahandler = datahandler
        self.dependencies: list[Metric] = []
        schema_filter = ""
        if entity_schema_ids:
            ids = ", ".join(str(int(entity_schema_id)) for entity_schema_id in entity_schema_ids)
            schema_filter = f"AND entity_schemas.entity_schema_id IN ({ids})"
        self.query = query.format(schema_filter=schema_filter)
        self.query_created = query_created.format(schema_filter=schema_filter)
        self.constraints: SchemaFulfillmentCount.CONSTRAINTS_TYPE = self.load_constraints()
        # Compliance of a value, keyed by (entity_schema_id, property_id, value as stored). is_value_compliant
        # is a pure function and the same values occur on millions of entities.
        self.compliance_cache: dict[tuple[int, int, str], bool] = {}
        if use_cache:
            self.cache: SchemaFulfillmentCount.DICT_TYPE = self.datahandler.get_cache_dict(self.metric_name, 3)
        else:
            self.cache: SchemaFulfillmentCount.DICT_TYPE = {}
        # Rows touched in the current timestep, mapped to their value before the
        # timestep. Lets dependent statistics update incrementally.
        self.changed_rows: SchemaFulfillmentCount.DICT_TYPE = {}

    def load_constraints(self) -> CONSTRAINTS_TYPE:
        """
        Reads the cardinalities and value constraints of all schema properties.
        A property whose value constraint is not valid JSON is left out, so that its edits are skipped.
        """
        constraints: SchemaFulfillmentCount.CONSTRAINTS_TYPE = {}
        for row in self.datahandler.query_duckdb_df(query_constraints).itertuples(index=False):
            key = (int(row.entity_schema_id), int(row.property_id))
            if key in constraints:
                print(f"Warning: (SchemaFulfillmentCount) Several constraint rows for schema {key[0]} property {key[1]}")
            value_constraint = None
            if row.value_constraint:
                try:
                    value_constraint = json.loads(row.value_constraint)
                except json.JSONDecodeError:
                    continue
            minimum = int(row.min) if pd.notna(row.min) else None
            maximum = int(row.max) if pd.notna(row.max) else None
            constraints[key] = (minimum, maximum, value_constraint)
        return constraints

    def log_memory(self):
        tracked_attrs = {
            "cache": self.cache,
            "constraints": self.constraints,
            "compliance_cache": self.compliance_cache,
            "changed_rows": self.changed_rows,
        }
        log_memory_snapshot(self.metric_name, tracked_attrs)

    def get_last_value(self) -> DICT_TYPE:
        return self.cache

    def get_changed_rows(self) -> DICT_TYPE:
        return self.changed_rows

    @staticmethod
    def is_value_compliant(value, value_constraint, property_id) -> bool:
        # If there is no constraint, pass
        # If the value constraint references another dict in the ShEx schema (given by its name), skip,
        # as we don't view the entire ShEx schema
        if value_constraint is None or type(value_constraint) == str:
            return True
        if type(value) not in (str, dict, float):
            print("Warning: (SchemaFulfillmentCount) Unhandled value type in schema constraint violation tester " + str(type(value)))
            return False
        if value_constraint["type"] == "NodeConstraint":
            if "values" in value_constraint:
                # contains a list of allowed values
                allowed_values = value_constraint["values"]
                is_value_allowed = False
                for v in allowed_values:
                    if type(v) == dict:
                        if v["type"] == "Language":
                            # We only expect to see english data
                            if v["languageTag"] != "en":
                                return False
                            else:
                                is_value_allowed = True
                                break
                        elif v["type"] == "IriStem":
                            if v["stem"] == "http://www.wikidata.org/entity" or v["stem"] == "http://www.wikidata.org/entity/":
                                if type(value) == str and len(value) > 1 and value[0] == "Q":
                                    is_value_allowed = True
                                    break
                                else:
                                    return False
                            elif v["stem"] == "http://commons.wikimedia.org/wiki/Special:FilePath":
                                # Cannot be verified, as only the filename is given, no reference or path.
                                is_value_allowed = True
                                break
                            else:
                                print("Warning: (SchemaFulfillmentCount) Unsupported IriStem prefix " + v["stem"])
                        else:
                            print("Warning: (SchemaFulfillmentCount) Unsupported type of allowed value constraint in entity schemas: " + v["type"])
                    elif type(v) == str:
                        # Format http://www.wikidata.org/entity/Q...
                        if type(value) == str and v.split("/")[-1] == value:
                            is_value_allowed = True
                            break
                    else:
                        print("Warning: (SchemaFulfillmentCount) Unsupported allowed value constraint in entity schemas: " + str(type(v)))
                if not is_value_allowed:
                    return False
            if "datatype" in value_constraint:
                match value_constraint["datatype"]:
                    case "http://www.opengis.net/ont/geosparql#wktLiteral": # coordinate
                        if not (type(value) == dict 
                            and "longitude" in value 
                            and "latitude" in value 
                            and type(value["longitude"]) in (float, int)
                            and type(value["latitude"]) in (float, int)
                        ):
                            return False
                    case "http://www.w3.org/2001/XMLSchema#dateTime": # date
                        try:
                            if value[0] == "+":
                                datetime.fromisoformat(value[1:])
                            else:
                                datetime.fromisoformat(value)
                        except Exception:
                            return False
                    case "http://www.w3.org/1999/02/22-rdf-syntax-ns#langString" | "http://www.w3.org/2001/XMLSchema#string": # string
                        if type(value) != str:
                            return False
                    case "http://www.w3.org/2001/XMLSchema#decimal": # number
                        try:
                            float(value)
                        except Exception:
                            return False
            return True
        elif value_constraint["type"] == "TripleConstraint":
            # Only traverse TripleConstraint if predicate matches the input property_id
            # We only look at one single triple at a time
            if (value_constraint["predicate"] == "http://www.wikidata.org/prop/statement/P" + str(property_id) 
                or value_constraint["predicate"] == "http://www.wikidata.org/prop/direct/P" + str(property_id)):
                return SchemaFulfillmentCount.is_value_compliant(value, value_constraint.get("valueExpr"), property_id)
            else:
                return True
        elif value_constraint["type"] == "EachOf":
            for expression in value_constraint["expressions"]:
                if not SchemaFulfillmentCount.is_value_compliant(value, expression, property_id):
                    return False
            return True
        elif value_constraint["type"] == "Shape":
            return SchemaFulfillmentCount.is_value_compliant(value, value_constraint.get("expression"), property_id)
        elif value_constraint["type"] == "ShapeOr":
            for expression in value_constraint["shapeExprs"]:
                if SchemaFulfillmentCount.is_value_compliant(value, expression, property_id):
                    return True
            return False
        elif value_constraint["type"] == "ShapeAnd":
            for expression in value_constraint["shapeExprs"]:
                if not SchemaFulfillmentCount.is_value_compliant(value, expression, property_id):
                    return False
            return True
        else:
            print("Warning: (SchemaFulfillmentCount) Unsupported constraint type in entity schemas: " + value_constraint["type"])
        
        return False

    def calculate_diff(self):
        # Not using calculate_diff due to performance reasons, but could be implemented in the future if needed
        pass

    def calculate(self):
        self.changed_rows = {}
        # Rows of created entities first, so that edits in the timestep of creation apply to them
        self.add_created_entities(self.datahandler.query_duckdb_df(self.query_created))
        self.update_cache(self.datahandler.query_duckdb_df(self.query))

    def add_created_entities(self, created: pd.DataFrame):
        """
        Adds a row without values for every schema property of the created entities.
        Rows that already exist are kept.
        """
        keys = zip(created.entity_schema_id, created.entity_id, created.property_id)
        for key, is_within_min_max in zip(keys, created.is_within_min_max):
            row = (0, 0, bool(is_within_min_max))
            if self.cache.setdefault(key, row) is row:
                # The row was added just now, so it did not exist before this timestep
                self.changed_rows[key] = (0, 0, None)

    def is_compliant(self, entity_schema_id: int, property_id: int, value_constraint, stored_value) -> bool:
        """Whether the stored (JSON encoded) value complies with the value constraint of the schema property."""
        if value_constraint is None:
            # Everything complies, so the value does not even have to be parsed
            return True
        key = (entity_schema_id, property_id, stored_value)
        compliant = self.compliance_cache.get(key)
        if compliant is None:
            compliant = self.is_value_compliant(json.loads(stored_value), value_constraint, property_id)
            if len(self.compliance_cache) >= COMPLIANCE_CACHE_LIMIT:
                self.compliance_cache.clear()
            self.compliance_cache[key] = compliant
        return compliant

    def update_cache(self, query_result):
        # Edits of entities without a creation row. The parser writes a creation row before the first
        # edit of every entity, so this should stay 0. Such entities only get rows for edited properties.
        num_edits_without_creation = 0
        for row in query_result.itertuples(index=False):
            entity_schema_id = int(row.entity_schema_id)
            entity_id = int(row.entity_id)
            property_id = int(row.property_id)
            constraint = self.constraints.get((entity_schema_id, property_id))
            if constraint is None:
                # The value constraint of the schema property could not be parsed
                continue
            min, max, value_constraint = constraint

            key = (entity_schema_id, entity_id, property_id)
            previous = self.cache.get(key)
            if previous is None:
                num_edits_without_creation += 1
                previous = (0, 0, None)
            num_compliant, num_noncompliant, _ = previous
            if pd.isna(num_compliant):
                num_compliant = 0
            if pd.isna(num_noncompliant):
                num_noncompliant = 0

            if row.action != "CREATE":
                if self.is_compliant(entity_schema_id, property_id, value_constraint, row.old_value):
                    num_compliant -= 1
                else:
                    num_noncompliant -= 1

            if row.action != "DELETE":
                if self.is_compliant(entity_schema_id, property_id, value_constraint, row.new_value):
                    num_compliant += 1
                else:
                    num_noncompliant += 1

            is_within_min_max = (min is None or num_compliant >= min) and (max is None or num_compliant <= max)

            self.changed_rows.setdefault(key, previous)
            self.cache[key] = (num_compliant, num_noncompliant, is_within_min_max)

        if num_edits_without_creation > 0:
            print(f"Warning: (SchemaFulfillmentCount) {num_edits_without_creation} schema edits on entities without a creation row")

    def write_result(self):
        # Is not written to database
        pass

    def save_cache(self):
        self.datahandler.save_cache_dict(self.metric_name, self.cache, "entity_schema_id INT, entity_id INT, property_id INT, num_compliant INT, num_noncompliant INT, is_within_min_max BOOLEAN, primary key (entity_schema_id, entity_id, property_id)")
