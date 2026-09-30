import json
import pandas as pd


def constraint_type_to_properties(df: pd.DataFrame) -> dict[str, list[str]]:
    # We remove the prefix, because in our data property ids are just numbers
    df = df.copy()
    df["property_id"] = df["property_id"].str.removeprefix("P")
    return (
        df.groupby("constraint_type")["property_id"]
          .agg(lambda s: list(set(s))) # remove duplicates(just in case) 
          .to_dict()
    )


if __name__ == "__main__":
    df = pd.read_csv(
        "data/property_constraints/property_constraints.csv",
        dtype=str,
        sep=";",
    )

    mapping = constraint_type_to_properties(df)

    with open(
        "data/property_constraints/constraint_type_to_properties.json",
        "w",
        encoding="utf-8",
    ) as f:
        json.dump(mapping, f, indent=2)
