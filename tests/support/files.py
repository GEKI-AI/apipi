import io
import zipfile
from typing import Any

from httpx import AsyncClient

from apipi.config import Settings
from apipi.gateway.auth import AuthRequest, tenant_from_key
from apipi.gateway.tokens import hash_token
from tests.support.http import auth

VISION_REGISTRY = {"test": {"input": ["text", "image"], "reasoning": True}}


class Users:
    def __call__(self, token: str, request: AuthRequest) -> dict[str, object]:
        user = request.headers.get("x-end-user")
        return {
            "key_id": hash_token(token),
            "tenant_id": tenant_from_key(token),
            "user_id": user,
            "cache_key": f"{token}:{user}",
        }

    def cache_key(self, token: str, request: AuthRequest) -> str:
        return f"{token}:{request.headers.get('x-end-user')}"


def vision(settings: Settings, **update: Any) -> Settings:
    return settings.model_copy(update={"model_registry": VISION_REGISTRY, **update})


def message(*parts: dict[str, Any]) -> dict[str, Any]:
    return {
        "events": [
            {
                "type": "agent.session.input.message",
                "input": [{"role": "user", "content": list(parts)}],
            }
        ]
    }


def input_file(file_id: str, **extra: Any) -> dict[str, Any]:
    return {"type": "input_file", "file_id": file_id, **extra}


def spy_commands(app: Any) -> list[dict[str, Any]]:
    hub = app.state.workers
    real = hub._send
    sent: list[dict[str, Any]] = []

    async def _send(conn: Any, wire: dict[str, Any]) -> None:
        if wire.get("type") == "command":
            sent.append(wire)
        await real(conn, wire)

    hub._send = _send
    return sent


def zip_skill(name: str = "demo", extra: dict[str, str] | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{name}/SKILL.md", f"---\nname: {name}\n---\nDo the thing.\n")
        for path, text in (extra or {}).items():
            archive.writestr(f"{name}/{path}", text)
    return buffer.getvalue()


async def upload_skill(client: AsyncClient, token: str, name: str = "demo") -> str:
    uploaded = await client.post(
        "/v1/skills",
        headers=auth(token),
        files={"files": (f"{name}.zip", zip_skill(name), "application/zip")},
    )
    assert uploaded.status_code == 200, uploaded.text
    return str(uploaded.json()["id"])
