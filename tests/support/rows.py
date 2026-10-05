import json


def row_json(row: object) -> str:
    table = getattr(row, "__table__", None)
    if table is None:
        return str(row)
    return json.dumps(
        {column.key: getattr(row, column.key) for column in table.columns},
        default=str,
    )
