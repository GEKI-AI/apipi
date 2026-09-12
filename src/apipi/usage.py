USAGE_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "total_tokens",
)

_PI_KEYS = {
    "prompt_tokens": ("prompt_tokens", "input"),
    "completion_tokens": ("completion_tokens", "output"),
    "cache_read_tokens": ("cache_read_tokens", "cacheRead"),
    "cache_write_tokens": ("cache_write_tokens", "cacheWrite"),
    "total_tokens": ("total_tokens", "totalTokens"),
}


def empty_usage() -> dict[str, int]:
    return {field: 0 for field in USAGE_FIELDS}


def token_count(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if value < 0:
        return 0
    return int(value)


def usage_from(raw: object | None) -> dict[str, int]:
    if not isinstance(raw, dict):
        return empty_usage()
    usage = empty_usage()
    for field, keys in _PI_KEYS.items():
        for key in keys:
            if key in raw:
                usage[field] = token_count(raw[key])
                break
    return usage


def add_usage(left: dict[str, int], right: dict[str, int]) -> dict[str, int]:
    return {field: left.get(field, 0) + right.get(field, 0) for field in USAGE_FIELDS}


def usage_from_messages(messages: object) -> dict[str, int] | None:
    if not isinstance(messages, list):
        return None
    total = empty_usage()
    found = False
    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "assistant":
            continue
        raw = message.get("usage")
        if not isinstance(raw, dict):
            continue
        total = add_usage(total, usage_from(raw))
        found = True
    if not found:
        return None
    return total
