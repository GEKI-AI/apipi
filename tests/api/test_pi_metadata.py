from httpx import AsyncClient


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_invalid_thinking_metadata_is_400(client: AsyncClient) -> None:
    token = "pi-thinking-bad"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "metadata": {"apipi.thinking": "ultra"}},
    )
    assert created.status_code == 400
    assert "thinking" in created.json()["error"]["message"]


async def test_reasoning_update_keeps_other_metadata(client: AsyncClient) -> None:
    token = "reasoning-merge"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "b",
            "model": "test",
            "metadata": {"team": "x", "apipi.system_prompt": "hi"},
        },
    )
    assert created.status_code == 200
    agent_id = created.json()["id"]
    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"reasoning": {"effort": "high"}},
    )
    assert updated.status_code == 200
    metadata = updated.json()["metadata"]
    assert metadata["team"] == "x"
    assert metadata["apipi.system_prompt"] == "hi"
    assert "apipi.thinking" not in metadata
    assert updated.json()["reasoning"]["effort"] == "high"
    cleared = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"reasoning": {"effort": None}},
    )
    assert cleared.status_code == 200
    assert cleared.json()["metadata"]["team"] == "x"
    assert "apipi.thinking" not in cleared.json()["metadata"]
    clash = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={
            "metadata": {"apipi.thinking": "low"},
            "reasoning": {"effort": "high"},
        },
    )
    assert clash.status_code == 400
    matched = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={
            "metadata": {"team": "x"},
            "reasoning": {"effort": "high"},
        },
    )
    assert matched.status_code == 200
    live = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert live.json()["metadata"]["team"] == "x"
    assert "apipi.thinking" not in live.json()["metadata"]
    assert live.json()["reasoning"]["effort"] == "high"


async def test_session_reasoning_update_replaces_effort(client: AsyncClient) -> None:
    token = "session-effort"
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={
            "name": "b",
            "model": "test",
            "reasoning": {"effort": "medium"},
        },
    )
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "agent": {"reasoning": {"effort": "low"}},
        },
    )
    assert created.status_code == 200
    assert created.json()["reasoning"]["effort"] == "low"
    raised = await client.post(
        f"/v1/agents/sessions/{created.json()['id']}",
        headers=_auth(token),
        json={"agent": {"reasoning": {"effort": "high"}}},
    )
    assert raised.status_code == 200
    assert raised.json()["reasoning"]["effort"] == "high"
    assert "apipi.thinking" not in raised.json()["metadata"]
    meta_only = await client.post(
        f"/v1/agents/sessions/{created.json()['id']}",
        headers=_auth(token),
        json={"metadata": {"team": "y"}},
    )
    assert meta_only.status_code == 200
    assert "apipi.thinking" not in meta_only.json()["metadata"]
    assert meta_only.json()["reasoning"]["effort"] == "high"
    reset = await client.post(
        f"/v1/agents/sessions/{created.json()['id']}",
        headers=_auth(token),
        json={"agent": {"reasoning": {"effort": None}}},
    )
    assert reset.status_code == 200
    assert "apipi.thinking" not in reset.json()["metadata"]
    assert reset.json()["reasoning"]["effort"] == "medium"
    kept = await client.post(
        f"/v1/agents/sessions/{created.json()['id']}",
        headers=_auth(token),
        json={"metadata": {"team": "z"}},
    )
    assert kept.status_code == 200
    assert kept.json()["metadata"]["team"] == "z"
    assert "apipi.thinking" not in kept.json()["metadata"]
    assert kept.json()["reasoning"]["effort"] == "medium"


async def test_service_tier_null_is_ignored(client: AsyncClient) -> None:
    token = "tier-null"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "b", "model": "test", "service_tier": None},
    )
    assert created.status_code == 200
    updated = await client.post(
        f"/v1/agents/{created.json()['id']}",
        headers=_auth(token),
        json={"service_tier": "auto", "reasoning": {"effort": "low"}},
    )
    assert updated.status_code == 200
    assert updated.json()["reasoning"]["effort"] == "low"
    session = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": created.json()["id"],
            "environment": {"type": "none"},
            "agent": {"service_tier": None, "reasoning": {"effort": "high"}},
        },
    )
    assert session.status_code == 200
    refused = await client.post(
        f"/v1/agents/{created.json()['id']}",
        headers=_auth(token),
        json={"service_tier": "flex"},
    )
    assert refused.status_code == 400
    assert refused.json()["error"]["type"] == "not_implemented"


async def test_reasoning_effort_is_stored_as_thinking(client: AsyncClient) -> None:
    token = "pi-reasoning"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "reasoning": {"effort": "high"}},
    )
    assert created.status_code == 200
    assert "apipi.thinking" not in created.json()["metadata"]
    assert created.json()["reasoning"]["effort"] == "high"
    summary = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "reasoning": {"summary": "auto"}},
    )
    assert summary.status_code == 400
    assert summary.json()["error"]["type"] == "not_implemented"


async def test_thinking_metadata_key_is_rejected(client: AsyncClient) -> None:
    token = "pi-thinking-removed"
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "reasoning": {"effort": "low"}},
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "metadata": {"apipi.thinking": "high"},
        },
    )
    assert created.status_code == 400
    assert "reasoning.effort" in created.json()["error"]["message"]


async def test_metadata_update_replaces_title(client: AsyncClient) -> None:
    token = "pi-title-replace"
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test"},
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "metadata": {"apipi.title": "old", "apipi.title_status": "done", "keep": 1},
        },
    )
    assert created.status_code == 200
    updated = await client.post(
        f"/v1/agents/sessions/{created.json()['id']}",
        headers=_auth(token),
        json={"metadata": {"keep": 1}},
    )
    assert updated.status_code == 200
    metadata = updated.json()["metadata"]
    assert metadata == {"keep": 1}
    assert "apipi.title" not in metadata
    assert "apipi.title_status" not in metadata


async def test_agent_metadata_round_trip_after_effort(client: AsyncClient) -> None:
    token = "roundtrip-agent"
    created = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "reasoning": {"effort": "high"}},
    )
    assert created.status_code == 200
    agent_id = created.json()["id"]
    assert "apipi.thinking" not in created.json()["metadata"]
    got = await client.get(f"/v1/agents/{agent_id}", headers=_auth(token))
    assert got.status_code == 200
    assert "apipi.thinking" not in got.json()["metadata"]
    assert got.json()["reasoning"]["effort"] == "high"
    echoed = dict(got.json()["metadata"])
    echoed["team"] = "x"
    updated = await client.post(
        f"/v1/agents/{agent_id}",
        headers=_auth(token),
        json={"metadata": echoed},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["metadata"]["team"] == "x"
    assert updated.json()["reasoning"]["effort"] == "high"


async def test_session_metadata_round_trip_after_effort(client: AsyncClient) -> None:
    token = "roundtrip-session"
    agent = await client.post(
        "/v1/agents",
        headers=_auth(token),
        json={"name": "bot", "model": "test", "reasoning": {"effort": "low"}},
    )
    assert agent.status_code == 200
    created = await client.post(
        "/v1/agents/sessions",
        headers=_auth(token),
        json={
            "agent_id": agent.json()["id"],
            "environment": {"type": "none"},
            "agent": {"reasoning": {"effort": "high"}},
        },
    )
    assert created.status_code == 200
    session_id = created.json()["id"]
    assert "apipi.thinking" not in created.json()["metadata"]
    got = await client.get(f"/v1/agents/sessions/{session_id}", headers=_auth(token))
    assert got.status_code == 200
    assert "apipi.thinking" not in got.json()["metadata"]
    assert got.json()["reasoning"]["effort"] == "high"
    echoed = dict(got.json()["metadata"])
    echoed["team"] = "y"
    updated = await client.post(
        f"/v1/agents/sessions/{session_id}",
        headers=_auth(token),
        json={"metadata": echoed},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["metadata"]["team"] == "y"
    assert updated.json()["reasoning"]["effort"] == "high"
