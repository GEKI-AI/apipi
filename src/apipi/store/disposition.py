from urllib.parse import quote

_ACTIVE_TYPES = frozenset(
    {
        "text/html",
        "application/xhtml+xml",
        "image/svg+xml",
        "text/xml",
        "application/xml",
        "text/javascript",
        "application/javascript",
    }
)


def content_disposition(name: str) -> str:
    cleaned = _basename(name)
    ascii_name = _ascii_name(cleaned)
    header = f'attachment; filename="{ascii_name}"'
    if any(ord(ch) > 127 for ch in cleaned):
        header += "; filename*=UTF-8''" + quote(cleaned, safe="")
    return header


def download_content_type(content_type: str | None) -> str | None:
    if content_type is None:
        return None
    raw = content_type.strip()
    if not raw:
        return None
    media = raw.split(";", 1)[0].strip().lower()
    if media in _ACTIVE_TYPES:
        return "application/octet-stream"
    return raw


def _basename(name: str) -> str:
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    kept = [ch for ch in base if ch >= " " and ch != "\x7f" and ch not in {'"', "\\"}]
    cleaned = "".join(kept).strip()
    if not cleaned or cleaned in {".", ".."}:
        return "download"
    return cleaned


def _ascii_name(name: str) -> str:
    fallback = "".join(ch if ord(ch) < 127 else "_" for ch in name).strip()
    if not fallback or fallback in {".", ".."}:
        return "download"
    return fallback
