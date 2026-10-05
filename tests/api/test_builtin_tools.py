from pathlib import Path

import pytest
from httpx import AsyncClient
from tests.support.http import auth, tenant_of

from apipi.config import Settings
from apipi.store.engine import Store
from apipi.store.repo import create_agent, ensure_tenant

pytest_plugins = ["tests.support.mcp_http_server"]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        mcp_allow_hosts="127.0.0.1",
    )


def _code(response) -> str | None:
    body = response.json()
    error = body.get("error")
    if isinstance(error, dict):
        return error.get("code")
    return None


async def test_agent_create_none_with_builtin_on_is_400(client: AsyncClient) -> None:
    token = "builtin-agent-none"
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.builtin_tools": "on"},
            "session_defaults": {"environment": {"type": "none"}},
        },
    )
    assert created.status_code == 400
    assert _code(created) == "builtin_tools"


async def test_agent_create_none_with_builtin_off_is_200(
    client: AsyncClient,
) -> None:
    token = "builtin-agent-none-off"
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.builtin_tools": "off"},
            "session_defaults": {"environment": {"type": "none"}},
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["metadata"]["apipi.builtin_tools"] == "off"


async def test_agent_create_invalid_builtin_value_is_400(
    client: AsyncClient,
) -> None:
    token = "builtin-bad-value"
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.builtin_tools": "sometimes"},
        },
    )
    assert created.status_code == 400
    assert _code(created) == "invalid_request"


async def test_agent_create_hosted_codemode_needs_builtin_tools(
    client: AsyncClient,
) -> None:
    token = "builtin-codemode-hosted"
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.builtin_tools": "off", "apipi.codemode": "on"},
        },
    )
    assert created.status_code == 400
    assert _code(created) == "builtin_tools"


async def test_agent_update_none_with_builtin_on_is_400(
    client: AsyncClient,
) -> None:
    token = "builtin-agent-update"
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "session_defaults": {"environment": {"type": "none"}},
        },
    )
    assert created.status_code == 200, created.text
    agent_id = created.json()["id"]
    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=auth(token),
        json={"metadata": {"apipi.builtin_tools": "on"}},
    )
    assert updated.status_code == 400
    assert _code(updated) == "builtin_tools"


async def test_agent_create_none_with_unknown_tool_type_is_400(
    client: AsyncClient,
) -> None:
    # The schema only knows function and mcp tool types, so anything else
    # is rejected at the boundary before the type=none check runs.
    token = "builtin-shell-tool"
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "tools": [{"type": "shell", "name": "bash"}],
            "session_defaults": {"environment": {"type": "none"}},
        },
    )
    assert created.status_code == 400


async def test_session_create_none_with_builtin_on_is_400(
    client: AsyncClient,
) -> None:
    token = "builtin-session-none"
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent": {"model": "test"},
            "environment": {"type": "none"},
            "metadata": {"apipi.builtin_tools": "on"},
        },
    )
    assert created.status_code == 400
    assert _code(created) == "builtin_tools"


async def test_session_create_none_with_codemode_on_is_400(
    client: AsyncClient,
) -> None:
    token = "builtin-session-codemode"
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent": {"model": "test"},
            "environment": {"type": "none"},
            "metadata": {"apipi.codemode": "on"},
        },
    )
    assert created.status_code == 400
    assert _code(created) == "builtin_tools"


async def test_session_create_none_with_unknown_tool_type_is_400(
    client: AsyncClient,
) -> None:
    token = "builtin-session-tool"
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent": {
                "model": "test",
                "tools": [{"type": "shell", "name": "bash"}],
            },
            "environment": {"type": "none"},
        },
    )
    assert created.status_code == 400


async def test_hosted_agent_used_for_none_session_is_rejected(
    client: AsyncClient,
) -> None:
    token = "builtin-hosted-agent"
    agent = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "tools": [
                {
                    "type": "function",
                    "name": "echo",
                    "description": "echo",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    )
    assert agent.status_code == 200, agent.text
    agent_id = agent.json()["id"]
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 200, created.text


async def test_legacy_hosted_agent_with_shell_tool_used_for_none_is_rejected(
    client: AsyncClient, store: Store
) -> None:
    # Agents stored before tool validation carry raw tool rows. Using one
    # with a non-allowed tool for a type=none session is tool_not_allowed.
    token = "builtin-legacy-agent"
    tenant_id = tenant_of(token)
    async with store.session() as db:
        await ensure_tenant(db, tenant_id)
        agent = await create_agent(
            db,
            tenant_id,
            name="legacy",
            model="test",
            tools=[{"type": "shell", "name": "bash"}],
        )
        agent_id = str(agent.id)
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 400
    assert _code(created) == "tool_not_allowed"


async def test_hosted_agent_with_builtin_on_used_for_none_is_rejected(
    client: AsyncClient,
) -> None:
    token = "builtin-hosted-on"
    agent = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.builtin_tools": "on"},
        },
    )
    assert agent.status_code == 200, agent.text
    agent_id = agent.json()["id"]
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent_id": agent_id, "environment": {"type": "none"}},
    )
    assert created.status_code == 400
    assert _code(created) == "builtin_tools"


async def test_session_create_none_with_function_and_http_mcp_runs(
    client: AsyncClient, mcp_server: tuple[str, dict[str, str]]
) -> None:
    mcp_url, _seen = mcp_server
    token = "builtin-session-ok"
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent": {
                "model": "test",
                "tools": [
                    {
                        "type": "function",
                        "name": "echo",
                        "description": "echo",
                        "parameters": {"type": "object", "properties": {}},
                    },
                    {
                        "type": "mcp",
                        "server_label": "mock",
                        "server_url": mcp_url,
                    },
                ],
            },
            "environment": {"type": "none"},
            "metadata": {"apipi.builtin_tools": "off"},
            "input": "hello",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "idle"


async def test_session_update_none_with_builtin_on_is_400(
    client: AsyncClient,
) -> None:
    token = "builtin-session-update"
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent": {"model": "test"},
            "environment": {"type": "none"},
            "input": "hello",
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    updated = await client.post(
        f"/v1/agents/sessions/{session_id}",
        headers=auth(token),
        json={"metadata": {"apipi.builtin_tools": "on"}},
    )
    assert updated.status_code == 400
    assert _code(updated) == "builtin_tools"


async def test_session_create_hosted_builtin_off_with_codemode_is_400(
    client: AsyncClient,
) -> None:
    token = "builtin-hosted-codemode"
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent": {"model": "test"},
            "metadata": {
                "apipi.builtin_tools": "off",
                "apipi.codemode": "on",
            },
        },
    )
    assert created.status_code == 400
    assert _code(created) == "builtin_tools"


async def test_session_create_hosted_builtin_off_runs_without_tools(
    client: AsyncClient,
) -> None:
    token = "builtin-hosted-off"
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent": {"model": "test"},
            "metadata": {"apipi.builtin_tools": "off"},
            "input": "hello",
        },
    )
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "idle"


async def test_builtin_tools_session_overrides_agent_both_ways(
    client: AsyncClient,
) -> None:
    token = "builtin-override"
    off_agent = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "off-bot",
            "model": "test",
            "metadata": {"apipi.builtin_tools": "off"},
        },
    )
    assert off_agent.status_code == 200, off_agent.text
    on_session = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": off_agent.json()["id"],
            "metadata": {"apipi.builtin_tools": "on"},
            "input": "hello",
        },
    )
    assert on_session.status_code == 200, on_session.text
    on_agent = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "on-bot",
            "model": "test",
            "metadata": {"apipi.builtin_tools": "on"},
        },
    )
    assert on_agent.status_code == 200, on_agent.text
    off_session = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={
            "agent_id": on_agent.json()["id"],
            "metadata": {"apipi.builtin_tools": "off"},
            "input": "hello",
        },
    )
    assert off_session.status_code == 200, off_session.text


async def test_session_update_hosted_codemode_needs_builtin_tools(
    client: AsyncClient,
) -> None:
    token = "builtin-hosted-update"
    created = await client.post(
        "/v1/agents/sessions",
        headers=auth(token),
        json={"agent": {"model": "test"}, "input": "hello"},
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["id"]
    updated = await client.post(
        f"/v1/agents/sessions/{session_id}",
        headers=auth(token),
        json={
            "metadata": {
                "apipi.builtin_tools": "off",
                "apipi.codemode": "on",
            }
        },
    )
    assert updated.status_code == 400
    assert _code(updated) == "builtin_tools"


async def test_bundle_export_keeps_builtin_tools(client: AsyncClient) -> None:
    import io
    import json
    import zipfile

    token = "builtin-export"
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.builtin_tools": "off"},
        },
    )
    assert created.status_code == 200, created.text
    exported = await client.get(
        f"/v1/apipi/agents/{created.json()['id']}/export",
        headers=auth(token),
    )
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        manifest = json.loads(archive.read("agent.json"))
    assert manifest["agent"]["metadata"]["apipi.builtin_tools"] == "off"


async def test_template_import_none_with_builtin_on_is_400(
    client: AsyncClient,
) -> None:
    import io
    import json
    import zipfile

    token = "builtin-template"
    created = await client.post(
        "/v1/agents",
        headers=auth(token),
        json={
            "name": "bot",
            "model": "test",
            "metadata": {"apipi.builtin_tools": "on"},
        },
    )
    assert created.status_code == 200, created.text
    exported = await client.get(
        f"/v1/apipi/agents/{created.json()['id']}/export",
        headers=auth(token),
    )
    assert exported.status_code == 200, exported.text
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        names = archive.namelist()
        manifest = json.loads(archive.read("agent.json"))
    assert isinstance(manifest["agent"], dict)
    manifest["agent"]["session_defaults"] = {"environment": {"type": "none"}}
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(exported.content)) as archive:
        blobs = {name: archive.read(name) for name in names if name != "agent.json"}
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("agent.json", json.dumps(manifest, indent=2) + "\n")
        for name, blob in blobs.items():
            archive.writestr(name, blob)
    imported = await client.post(
        "/v1/apipi/templates/import",
        headers=auth(token),
        files={
            "bundle": (
                "agent.apipi-agent.zip",
                buf.getvalue(),
                "application/zip",
            )
        },
    )
    assert imported.status_code == 400
    assert _code(imported) == "builtin_tools"
