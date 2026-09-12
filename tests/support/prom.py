def metric_line(body: str, metric: str, **labels: str) -> str:
    wanted = set(labels.items())
    prefix = f"{metric}{{"
    for line in body.splitlines():
        if not line.startswith(prefix):
            continue
        head, _, value = line.partition("} ")
        if not value:
            continue
        inner = head[len(prefix) :]
        got: set[tuple[str, str]] = set()
        if inner:
            for part in inner.split(","):
                key, raw = part.split("=", 1)
                got.add((key, raw.strip('"')))
        if got == wanted:
            return line
    raise AssertionError(f"missing {metric} {labels} in {body}")
