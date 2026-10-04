"""The command context.

`turn.start`, `turn.continue` and `sandbox.boot` carry a `context`
object built by the API. The worker holds it in memory only and never
logs it. Model keys and environment credential values are excluded
from the model repr and replaced by `redact_context`. File bytes never
travel in the command: files, skills and the Pi session blob travel as
references only, either presigned GET URLs (S3 store) or relative
paths inside the shared local store root (filesystem store).
"""

import copy
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pydantic import Field, ValidationError

from apipi.protocol.base import ContextPart
from apipi.protocol.constants import (
    COMMAND_CONTEXT_OPS,
    MAX_COMMAND_BYTES,
    wire_size,
)


class CommandTooLarge(ValueError):
    def __init__(self, size: int, limit: int = MAX_COMMAND_BYTES) -> None:
        super().__init__(f"worker command is {size} bytes, limit is {limit}")
        self.size = size
        self.limit = limit


class ContextBytes(ValueError):
    pass


class ContextModel(ContextPart):
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)


class ContextMcpServer(ContextPart):
    server_label: str
    server_url: str
    headers: dict[str, str] = Field(default_factory=dict)
    allowed_tools: list[str] = Field(default_factory=list)


class ContextEnvCredential(ContextPart):
    credential_id: str
    secret_name: str
    secret_value: str = Field(repr=False)
    allowed_hosts: list[str] = Field(default_factory=list)
    git_username: str | None = None


class ContextFileRef(ContextPart):
    path: str
    object_id: str
    url: str | None = None
    local_path: str | None = None
    size_bytes: int | None = None
    content_type: str | None = None


class ContextSkillRef(ContextPart):
    skill_id: str
    object_id: str
    url: str | None = None
    local_path: str | None = None


class ContextPiSession(ContextPart):
    present: bool = False
    object_id: str | None = None
    url: str | None = None
    local_path: str | None = None


class ContextSession(ContextPart):
    environment: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    required_actions: list[Any] = Field(default_factory=list)
    status: str | None = None
    user_id: str | None = None
    org_id: str | None = None
    key_id: str = ""
    agent_id: str | None = None
    idle_ttl_seconds: float | None = None


class ContextAgent(ContextPart):
    model: str | None = None
    instructions: str | None = None
    function_tools: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)
    builtin_tools: str = "on"
    codemode: str = "off"
    thinking: str | None = None
    web_search: bool = False


class TurnContext(ContextPart):
    session: ContextSession
    agent: ContextAgent = Field(default_factory=ContextAgent)
    model: ContextModel = Field(default_factory=ContextModel)
    mcp: list[ContextMcpServer] = Field(default_factory=list)
    env_credentials: list[ContextEnvCredential] = Field(default_factory=list)
    files: list[ContextFileRef] = Field(default_factory=list)
    session_files: list[ContextFileRef] = Field(default_factory=list)
    skills: list[ContextSkillRef] = Field(default_factory=list)
    pi_session: ContextPiSession = Field(default_factory=ContextPiSession)


def _reject_bytes(value: Any) -> None:
    if isinstance(value, bytes):
        raise ContextBytes("turn context must not contain file bytes")
    if isinstance(value, dict):
        for item in value.values():
            _reject_bytes(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_bytes(item)


def context_error_message(exc: Exception) -> str:
    if isinstance(exc, ValidationError):
        fields = sorted(
            {
                ".".join(str(part) for part in error.get("loc", ()))
                for error in exc.errors(include_input=False, include_url=False)
            }
        )
        return "invalid turn context: " + ", ".join(fields)
    return f"invalid turn context: {exc}"


def parse_turn_context(raw: Any) -> TurnContext:
    if not isinstance(raw, dict):
        raise ContextBytes("turn context must be an object")
    _reject_bytes(raw)
    return TurnContext.model_validate(raw)


def check_command_size(message: dict[str, Any]) -> int:
    """Check the wire size of one command frame (or its payload) in bytes."""
    size = wire_size(message)
    if size > MAX_COMMAND_BYTES:
        raise CommandTooLarge(size)
    return size


def check_context_op(op: str, payload: dict[str, Any]) -> None:
    if op not in COMMAND_CONTEXT_OPS:
        return
    raw = payload.get("context")
    if raw is None:
        return
    parse_turn_context(raw)
    check_command_size(payload)


def redact_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.query or parts.fragment:
        return urlunsplit((parts.scheme, parts.netloc, parts.path, "...", ""))
    return url


def redact_context(raw: Any) -> Any:
    """Return a copy safe for logs: secrets replaced, URLs unsigned."""
    redacted = copy.deepcopy(raw)
    if not isinstance(redacted, dict):
        return redacted
    model = redacted.get("model")
    if isinstance(model, dict) and model.get("api_key"):
        model["api_key"] = "..."
    servers = redacted.get("mcp")
    if isinstance(servers, list):
        for server in servers:
            if not isinstance(server, dict):
                continue
            headers = server.get("headers")
            if isinstance(headers, dict):
                for key in headers:
                    headers[key] = "..."
            url = server.get("server_url")
            if isinstance(url, str) and url:
                server["server_url"] = redact_url(url)
    credentials = redacted.get("env_credentials")
    if isinstance(credentials, list):
        for credential in credentials:
            if isinstance(credential, dict) and "secret_value" in credential:
                credential["secret_value"] = "..."
    for key in ("files", "session_files", "skills"):
        refs = redacted.get(key)
        if isinstance(refs, list):
            for ref in refs:
                if isinstance(ref, dict) and isinstance(ref.get("url"), str):
                    ref["url"] = redact_url(ref["url"])
    pi_session = redacted.get("pi_session")
    if isinstance(pi_session, dict) and isinstance(pi_session.get("url"), str):
        pi_session["url"] = redact_url(pi_session["url"])
    return redacted


def summarize_context(raw: dict[str, Any]) -> dict[str, Any]:
    """Small secret-free summary for log fields."""
    session = raw.get("session") if isinstance(raw, dict) else None
    agent = raw.get("agent") if isinstance(raw, dict) else None
    mcp = raw.get("mcp") if isinstance(raw, dict) else None
    environment: dict[str, Any] = {}
    if isinstance(session, dict) and isinstance(session.get("environment"), dict):
        environment = session["environment"]
    labels: list[str] = []
    if isinstance(mcp, list):
        for server in mcp:
            if isinstance(server, dict) and isinstance(server.get("server_label"), str):
                labels.append(server["server_label"])
    files = raw.get("files") if isinstance(raw, dict) else None
    session_files = raw.get("session_files") if isinstance(raw, dict) else None
    skills = raw.get("skills") if isinstance(raw, dict) else None
    credentials = raw.get("env_credentials") if isinstance(raw, dict) else None
    model: str | None = None
    if isinstance(agent, dict) and isinstance(agent.get("model"), str):
        model = agent["model"]
    return {
        "env_type": environment.get("type"),
        "model": model,
        "mcp_servers": labels,
        "file_count": len(files) if isinstance(files, list) else 0,
        "session_file_count": (
            len(session_files) if isinstance(session_files, list) else 0
        ),
        "skill_count": len(skills) if isinstance(skills, list) else 0,
        "env_credential_count": (
            len(credentials) if isinstance(credentials, list) else 0
        ),
    }
