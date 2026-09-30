import json
import os
import pandas as pd
from pyshex.utils.schema_loader import SchemaLoader
from dotenv import load_dotenv
import psycopg2

"""
Converts the ShEx entity schemas into a simplified format that
condenses the schemas to a list of constraints applying to a specific entity and property.
Loads the resulting CSV files into the database.
"""

def process_expression(expression) -> tuple[int, int | None, int | None, str] | None:
    if expression["predicate"] == "http://www.w3.org/2000/01/rdf-schema#label":
        property_id = -1
    elif expression["predicate"] == "http://www.w3.org/2000/01/rdf-schema#description":
        property_id = -2
    else:
        if expression["predicate"].startswith("http://www.wikidata.org/"):
            property_id = int(expression["predicate"].split("/")[-1][1:])
            # Only view the expression if it applies to a statement node or direct property
            if not expression["predicate"].startswith("http://www.wikidata.org/prop/P") and not expression["predicate"].startswith("http://www.wikidata.org/prop/direct/P"):
                return None
        else:
            # Skip items outside of Wikidata
            return None
    
    min = expression.get("min")
    max = expression.get("max")

    if min is None and max is None:
        # ShEx defaults to 'exactly 1'
        min = 1
        max = 1

    if min is not None:
        if min.is_integer():
            min = int(min)
        else:
            print("Error: Min is not an integer")
            raise Exception("Error: Min is not an integer")
    
    if max is not None:
        if max.is_integer():
            max = int(max)
        else:
            print("Error: Max is not an integer")
            raise Exception("Error: Max is not an integer")

    value_constraint = expression.get("valueExpr")
    if value_constraint:
        if type(value_constraint) == dict:
            value_constraint = json.dumps(value_constraint)

    return (property_id, min, max, value_constraint)
    

def parse_shex(shex: str, entity_schema_id: int, schema_rows: dict[tuple[int, int], set[tuple[int | None, int | None, str]]]) -> None:
    loader = SchemaLoader()
    s = loader.loads(shex)
    
    json_schema = json.loads(s._as_json)
    start = json_schema.get("start")

    if start is None:
        raise Exception("Error: No start shape")

    start_shape = None

    for shape in json_schema["shapes"]:
        if shape["id"] == start:
            start_shape = shape
            break

    if start_shape is None:
        raise Exception("Error: No start shape")

    if start_shape["type"] == "Shape":
        shape_expressions = [start_shape]
    elif start_shape["type"] == "ShapeAnd":
        shape_expressions = start_shape["shapeExprs"]
    elif start_shape["type"] == "ShapeOr":
        print("Skipped due to ShapeOr shape type")
        return
    else:
        raise Exception("Error: Unsupported shape type " + start_shape["type"])
    
    for shape_expression in shape_expressions:
        exclude_predicates = [int(p.split("/")[-1][1:]) for p in shape_expression.get("extra", [])]
        if shape_expression["type"] == "Shape":
            shape_expression = shape_expression["expression"]
        if shape_expression["type"] == "EachOf":
            for expression in shape_expression["expressions"]:
                result = process_expression(expression)
                if result is not None and result[0] not in exclude_predicates:
                    key = (entity_schema_id, result[0])
                    value = result[1:]
                    if key in schema_rows:
                        schema_rows[key].add(value)
                    else:
                        schema_rows[key] = set((value,))

        elif shape_expression["type"] == "TripleConstraint":
            result = process_expression(shape_expression)
            if result is not None and result[0] not in exclude_predicates:
                key = (entity_schema_id, result[0])
                value = result[1:]
                if key in schema_rows:
                    schema_rows[key].add(value)
                else:
                    schema_rows[key] = set((value,))
        else:
            raise Exception("Error: Unhandled expression type " + shape_expression["type"])
    
def main():
    successful = 0
    not_successful = 0
    schema_rows: dict[tuple[int, int], set[tuple[int | None, int | None, str]]] = {}
    for file in os.listdir(os.path.dirname(__file__) + "/entity_schemas"):
        if file.endswith(".shex"):
            with open(f"{os.path.dirname(__file__)}/entity_schemas/{file}", "r", encoding="utf-8") as f:
                shex = f.read()
                try:
                    parse_shex(shex, int(file[:-5].split("_")[-1][1:]), schema_rows)
                    successful += 1
                except Exception as e:
                    # print(file, e)
                    not_successful += 1

    merged_schema_rows: list[tuple[int, int, int | None, int | None, str]] = []
    for (entity_schema_id, property_id), v in schema_rows.items():
        aggregated_min = None
        aggregated_max = None
        aggregated_value_constraints: dict = {
            "type": "ShapeAnd",
            "shapeExprs": []
        }

        for (min_, max_, value_constraint_) in v:
            # Get min max
            if max_ == -1:
                max_ = None
            if min_ is not None:
                if aggregated_min is None:
                    aggregated_min = min_
                else:
                    aggregated_min = max(aggregated_min, min_)
            if max_ is not None:
                if aggregated_max is None:
                    aggregated_max = max_
                else:
                    aggregated_max = min(aggregated_max, max_)

            # Aggregate value constraints
            if value_constraint_:
                try:
                    value_constraint_json = json.loads(value_constraint_)
                    aggregated_value_constraints["shapeExprs"].append(value_constraint_json)
                except json.JSONDecodeError:
                    pass
        
        # Can be either a compound of multiple expressions merged via ShapeAnd shape type (len > 1),
        # a single entry, or empty
        # If it is a single entry, extract it and store it without the ShapeAnd wrapper.
        value_constraints_str = ""
        if len(aggregated_value_constraints["shapeExprs"]) == 1:
            aggregated_value_constraints = aggregated_value_constraints["shapeExprs"][0]
            value_constraints_str = json.dumps(aggregated_value_constraints)
        elif len(aggregated_value_constraints["shapeExprs"]) > 1:
            value_constraints_str = json.dumps(aggregated_value_constraints)
        
        if value_constraints_str or aggregated_max or (aggregated_min and aggregated_min > 0):
            merged_schema_rows.append((entity_schema_id, property_id, aggregated_min, aggregated_max, value_constraints_str))
        
    df = pd.DataFrame(merged_schema_rows, columns=["entity_schema_id", "property_id", "min", "max", "value_constraint"])
    df["min"] = df["min"].astype("Int64")
    df["max"] = df["max"].astype("Int64")
    df.to_csv(f"{os.path.dirname(__file__)}/simple_schema_rows.csv", index=False, sep=";")

    dotenv_path = ".env"
    load_dotenv(dotenv_path)

    # credentials for DB connection
    DB_USER = os.environ.get("DB_USER")
    DB_PASS = os.environ.get("DB_PASS")
    DB_NAME = os.environ.get("DB_NAME")
    DB_HOST = os.environ.get("DB_HOST")
    DB_PORT = os.environ.get("DB_PORT")
    
    conn = psycopg2.connect(
        dbname=DB_NAME,
        user=DB_USER,
        password=DB_PASS, 
        host=DB_HOST,
        port=DB_PORT,
        connect_timeout=30,
        gssencmode='disable'
    )

    with conn.cursor() as cur:
        with open("schema_parsing/entity_schema_table.sql", "r") as f:
            copy_tables_sql = f.read().replace("<WORKDIR>", os.path.dirname(__file__))
            cur.execute(copy_tables_sql)
    conn.commit()
    
    print("Parsing of entity schemas finished")
    print("Successful: " + str(successful))
    print("Unsuccessful: " + str(not_successful))


if __name__ == "__main__":
    main()
    