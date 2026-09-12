from uuid import UUID

calls: list[str] = []

TENANT = UUID("12345678-1234-5678-1234-567812345678")


def reset() -> None:
    calls.clear()


def accept(bearer: str) -> dict[str, str]:
    calls.append(bearer)
    return {"key_id": "plugin", "tenant_id": str(TENANT)}


def reject(bearer: str) -> None:
    calls.append(bearer)
    return None


def boom(bearer: str) -> None:
    calls.append(bearer)
    raise RuntimeError("plugin failed")
