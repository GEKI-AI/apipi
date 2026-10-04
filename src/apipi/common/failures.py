import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

FIXTURE_PI = "1.0.0"

_OVERFLOW = (
    re.compile(r"prompt (?:is )?too long", re.I),
    re.compile(r"prompt exceeds max length", re.I),
    re.compile(r"request_too_large", re.I),
    re.compile(r"input is too long for requested model", re.I),
    re.compile(r"exceeds the context window", re.I),
    re.compile(
        r"exceeds (?:the )?(?:model'?s )?maximum context length"
        r"(?: of [\d,]+ tokens?|\s*\([\d,]+\))",
        re.I,
    ),
    re.compile(r"input token count.*exceeds the maximum", re.I),
    re.compile(r"maximum prompt length is \d+", re.I),
    re.compile(r"reduce the length of the messages", re.I),
    re.compile(r"maximum context length is \d+ tokens", re.I),
    re.compile(
        r"exceeds (?:the )?maximum allowed input length of [\d,]+ tokens?",
        re.I,
    ),
    re.compile(
        r"input \(\d+ tokens\) is longer than the model'?s context length"
        r" \(\d+ tokens\)",
        re.I,
    ),
    re.compile(r"exceeds the limit of \d+", re.I),
    re.compile(r"exceeds the available context size", re.I),
    re.compile(r"greater than the context length", re.I),
    re.compile(r"context window exceeds limit", re.I),
    re.compile(r"exceeded model token limit", re.I),
    re.compile(r"too large for model with \d+ maximum context length", re.I),
    re.compile(
        r"prompt has [\d,]+ tokens?, but the configured context size is"
        r" [\d,]+ tokens?",
        re.I,
    ),
    re.compile(r"model_context_window_exceeded", re.I),
    re.compile(r"prompt too long; exceeded (?:max )?context length", re.I),
    re.compile(r"range of input length should be", re.I),
    re.compile(r"context[_ ]length[_ ]exceeded", re.I),
    re.compile(r"too many tokens", re.I),
    re.compile(r"token limit exceeded", re.I),
    re.compile(r"^4(?:00|13)\s*(?:status code)?\s*\(no body\)", re.I),
)
_NON_OVERFLOW = (
    re.compile(r"^(Throttling error|Service unavailable):", re.I),
    re.compile(r"rate limit", re.I),
    re.compile(r"too many requests", re.I),
)
_LEADING_STATUS = re.compile(r"^(\d{3})(?=[\s:])")
_PAREN_STATUS = re.compile(r"\((\d{3})\)")
_RATE = re.compile(r"rate[ ._]?limit|too many requests", re.I)
_TIMEOUT = re.compile(r"timed?\s*out|\btimeout\b", re.I)
_CONNECTION = re.compile(
    r"connection error|econnrefused|econnreset|fetch failed|"
    r"connection refused|connection reset|getaddrinfo|enotfound|"
    r"eai_again|provider finish_reason:\s*network_error",
    re.I,
)
_FILTER = re.compile(r"provider finish_reason:\s*content_filter", re.I)
_NO_BODY = re.compile(r"status code \(no body\)", re.I)

# code -> (failure_source, retryable)
_KNOWN: dict[str, tuple[str, bool]] = {
    "upstream_rate_limited": ("upstream", True),
    "upstream_5xx": ("upstream", True),
    "upstream_timeout": ("upstream", True),
    "upstream_connection": ("upstream", True),
    "upstream_unauthorized": ("upstream", False),
    "context_length_exceeded": ("upstream", False),
    "upstream_content_filter": ("upstream", False),
    "upstream_4xx": ("upstream", False),
    "upstream_error": ("upstream", False),
    "cancelled": ("user", False),
    "client_disconnected": ("user", False),
    "invalid_request": ("user", False),
    "model_required": ("user", False),
    "model_not_found": ("user", False),
    "model_host_unreachable": ("user", False),
    "model_host_unauthorized": ("user", False),
    "payload_too_large": ("user", False),
    "not_found": ("user", False),
    "unknown_field": ("user", False),
    "validation_error": ("user", False),
    "workspace_too_large": ("user", False),
    "artifact_too_large": ("user", False),
    "gone": ("user", False),
    "unauthorized": ("user", False),
    "tool_not_allowed": ("user", False),
    "builtin_tools": ("user", False),
    "credential_not_allowed": ("user", False),
    "credential_host_not_allowed": ("user", False),
    "secret_name_collision": ("user", False),
    "forward_models": ("user", False),
    "presign_unsupported": ("user", False),
    "capacity": ("user", False),
    "capacity_tenant": ("user", False),
    "turn_timeout": ("internal", True),
    "pi_exited": ("internal", True),
    "pi_memory": ("internal", False),
    "spawn_failed": ("internal", True),
    "sandbox_boot_failed": ("internal", True),
    "attachment_push_failed": ("internal", True),
    "artifact_store": ("internal", True),
    "worker_lease_expired": ("internal", True),
    "worker_command_timeout": ("internal", True),
    "worker_outbox_full": ("internal", True),
    "worker_message_too_large": ("internal", False),
    "turn_interrupted": ("internal", True),
    "internal": ("internal", False),
    "placement": ("internal", False),
    "image_unavailable": ("internal", True),
}

_LEGACY = frozenset({"pi_exited", "pi_memory"})
_INFO = frozenset({"cancelled", "client_disconnected"})
_UPSTREAM_WARNING = frozenset(
    {
        "upstream_rate_limited",
        "upstream_unauthorized",
        "context_length_exceeded",
        "upstream_content_filter",
        "upstream_4xx",
    }
)
_CLASS_KEYS = (
    "failure_source",
    "detail_code",
    "upstream_status",
    "retryable",
    "legacy_code",
    "upstream_attempts",
)


@dataclass(frozen=True)
class Failure:
    message: str
    code: str
    failure_source: str
    retryable: bool
    upstream_status: int | None = None
    legacy_code: str | None = None
    upstream_attempts: int | None = None

    def public_code(self, mode: str) -> str:
        if mode == "specific" or not self.legacy_code:
            return self.code
        return self.legacy_code


def failure_dict(failure: Failure) -> dict[str, Any]:
    """Serialize a failure for the worker `usage` envelope."""
    return {
        "message": failure.message,
        "code": failure.code,
        "failure_source": failure.failure_source,
        "retryable": failure.retryable,
        "upstream_status": failure.upstream_status,
        "legacy_code": failure.legacy_code,
        "upstream_attempts": failure.upstream_attempts,
    }


def failure_from_dict(data: Mapping[str, Any]) -> Failure:
    """Rebuild a failure the worker serialized with :func:`failure_dict`."""
    return Failure(
        message=str(data.get("message") or ""),
        code=str(data.get("code") or "internal"),
        failure_source=str(data.get("failure_source") or "internal"),
        retryable=bool(data.get("retryable", False)),
        upstream_status=(
            int(data["upstream_status"])
            if isinstance(data.get("upstream_status"), int)
            else None
        ),
        legacy_code=(
            str(data["legacy_code"])
            if isinstance(data.get("legacy_code"), str)
            else None
        ),
        upstream_attempts=(
            int(data["upstream_attempts"])
            if isinstance(data.get("upstream_attempts"), int)
            else None
        ),
    )


def error_mode(settings: object | None) -> str:
    mode = getattr(settings, "error_codes", None)
    if mode == "specific":
        return "specific"
    return "legacy"


def failure_for(
    code: str,
    message: str,
    *,
    upstream_status: int | None = None,
) -> Failure:
    if code == "model_host_error":
        return classify_host_message(message)
    source, retryable = _KNOWN.get(code, ("internal", False))
    return Failure(
        message=message,
        code=code,
        failure_source=source,
        retryable=retryable,
        upstream_status=upstream_status,
        legacy_code=_legacy(code, source),
    )


def classify_host_message(raw: object) -> Failure:
    text = raw.strip() if isinstance(raw, str) else ""
    status = _status(text)
    if _FILTER.search(text):
        code = "upstream_content_filter"
    elif status in (401, 403):
        code = "upstream_unauthorized"
    elif status == 429 or (status is None and _RATE.search(text)):
        code = "upstream_rate_limited"
    elif _overflow(text) and status in (None, 400, 413):
        code = "context_length_exceeded"
    elif (
        status == 408
        or _timeout_504(text, status)
        or (status is None and _TIMEOUT.search(text))
    ):
        code = "upstream_timeout"
    elif status is not None and 500 <= status <= 599:
        code = "upstream_5xx"
    elif status is None and _CONNECTION.search(text):
        code = "upstream_connection"
    elif status is not None and 400 <= status <= 499:
        code = "upstream_4xx"
    else:
        code = "upstream_error"
        status = None
    source, retryable = _KNOWN[code]
    return Failure(
        message=_public_message(text, status),
        code=code,
        failure_source=source,
        retryable=retryable,
        upstream_status=status,
        legacy_code="model_host_error",
    )


def log_level_for(failure: Failure) -> int:
    return log_level_for_code(failure.code, failure.failure_source)


def log_level_for_code(code: str, source: str | None = None) -> int:
    if code in _INFO:
        return logging.INFO
    resolved = source
    if resolved is None:
        resolved = _KNOWN.get(code, ("internal", False))[0]
    if resolved == "user":
        return logging.WARNING
    if code in _UPSTREAM_WARNING:
        return logging.WARNING
    return logging.ERROR


def pi_payload(failure: Failure) -> dict[str, Any]:
    return {
        "message": failure.message,
        "code": failure.code,
        "failure_source": failure.failure_source,
        "upstream_status": failure.upstream_status,
        "retryable": failure.retryable,
        "legacy_code": failure.legacy_code,
    }


def failure_from_payload(data: dict[str, Any]) -> Failure:
    code = data.get("code")
    source = data.get("failure_source")
    message = data.get("message")
    text = message if isinstance(message, str) and message else "Model host error"
    if isinstance(code, str) and code and isinstance(source, str) and source:
        status = data.get("upstream_status")
        legacy = data.get("legacy_code")
        attempts = data.get("upstream_attempts")
        return Failure(
            message=text,
            code=code,
            failure_source=source,
            retryable=bool(data.get("retryable")),
            upstream_status=status if isinstance(status, int) else None,
            legacy_code=legacy if isinstance(legacy, str) and legacy else None,
            upstream_attempts=(
                attempts if isinstance(attempts, int) and attempts >= 0 else None
            ),
        )
    return classify_host_message(text)


def turn_failed_data(turn_id: str, failure: Failure) -> dict[str, Any]:
    data: dict[str, Any] = {
        "turn_id": turn_id,
        "message": failure.message,
        "code": failure.code,
        "failure_source": failure.failure_source,
        "upstream_status": failure.upstream_status,
        "retryable": failure.retryable,
    }
    if failure.legacy_code:
        data["legacy_code"] = failure.legacy_code
    if failure.upstream_attempts is not None:
        data["upstream_attempts"] = failure.upstream_attempts
    return data


def session_error_data(failure: Failure, *, mode: str) -> dict[str, Any]:
    data: dict[str, Any] = {
        "message": failure.message,
        "code": failure.public_code(mode),
        "detail_code": failure.code,
        "failure_source": failure.failure_source,
        "upstream_status": failure.upstream_status,
        "retryable": failure.retryable,
    }
    if failure.legacy_code:
        data["legacy_code"] = failure.legacy_code
    if failure.upstream_attempts is not None:
        data["upstream_attempts"] = failure.upstream_attempts
    return data


def cancel_data(turn_id: str) -> dict[str, Any]:
    return {
        "turn_id": turn_id,
        "failure_source": "user",
        "code": "cancelled",
        "reason": "user",
        "upstream_status": None,
        "retryable": False,
    }


def log_extra(failure: Failure) -> dict[str, Any]:
    fields: dict[str, Any] = {
        "error_code": failure.code,
        "failure_source": failure.failure_source,
        "upstream_status": failure.upstream_status,
        "retryable": failure.retryable,
    }
    if failure.legacy_code:
        fields["legacy_code"] = failure.legacy_code
    if failure.upstream_attempts is not None:
        fields["upstream_attempts"] = failure.upstream_attempts
    return fields


def usage_fields(failure: Failure | None) -> dict[str, Any]:
    if failure is None:
        return {
            "failure_source": None,
            "upstream_status": None,
            "retryable": None,
            "legacy_code": None,
            "upstream_attempts": None,
        }
    return {
        "failure_source": failure.failure_source,
        "upstream_status": failure.upstream_status,
        "retryable": failure.retryable,
        "legacy_code": failure.legacy_code,
        "upstream_attempts": failure.upstream_attempts,
    }


def error_extra(data: dict[str, Any]) -> dict[str, Any]:
    extra: dict[str, Any] = {}
    for key in _CLASS_KEYS:
        if key in data:
            extra[key] = data[key]
    return extra


def _legacy(code: str, source: str) -> str | None:
    if source == "upstream" or code in _LEGACY:
        return "model_host_error"
    return None


def _status(text: str) -> int | None:
    if not text:
        return None
    match = _LEADING_STATUS.match(text)
    if match is None:
        match = _PAREN_STATUS.search(text)
    if match is None:
        return None
    code = int(match.group(1))
    if 400 <= code <= 599:
        return code
    return None


def _overflow(text: str) -> bool:
    if not text:
        return False
    if any(pattern.search(text) for pattern in _NON_OVERFLOW):
        return False
    return any(pattern.search(text) for pattern in _OVERFLOW)


def _timeout_504(text: str, status: int | None) -> bool:
    if status != 504:
        return False
    body = _body_after(text, 504)
    if not body or _NO_BODY.search(body) is not None:
        return True
    return _TIMEOUT.search(text) is not None


def _body_after(text: str, status: int) -> str:
    match = re.match(rf"^{status}(?:\s*:\s*|\s+)(.*)$", text.strip(), re.S)
    if match is None:
        return ""
    return match.group(1).strip()


def _public_message(text: str, status: int | None) -> str:
    if status in (401, 403):
        return f"Model host error ({status})"
    if not text:
        return "Model host error"
    line = text.split("\n")[0][:300]
    lowered = line.lower()
    if "api key" in lowered or "bearer " in lowered:
        return "Model host error"
    return line


def session_failed_data(message: str, code: str | None) -> dict[str, Any]:
    if code:
        return session_error_data(failure_for(code, message), mode="legacy")
    return {"message": message}
