import json
import sys
from pathlib import Path
from typing import Any

from apipi.protocol.schema import message_schemas

SCHEMA_DIR = (
    Path(__file__).resolve().parent.parent / "docs" / "worker-protocol" / "schema"
)


def render(schema: dict[str, Any]) -> str:
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def main() -> int:
    SCHEMA_DIR.mkdir(parents=True, exist_ok=True)
    schemas = message_schemas()
    for stale in SCHEMA_DIR.glob("*.json"):
        if stale.stem not in schemas:
            stale.unlink()
    for name, schema in schemas.items():
        (SCHEMA_DIR / f"{name}.json").write_text(render(schema), encoding="utf-8")
    print(f"wrote {len(schemas)} schemas to {SCHEMA_DIR}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
