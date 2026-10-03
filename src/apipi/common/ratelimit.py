"""A per-second rate budget."""


def relay_rate_allowed(hits: list[float], *, now: float, limit: int) -> bool:
    """Record one hit and say whether the per-second budget holds."""
    cutoff = now - 1.0
    while hits and hits[0] <= cutoff:
        hits.pop(0)
    hits.append(now)
    return len(hits) <= limit
