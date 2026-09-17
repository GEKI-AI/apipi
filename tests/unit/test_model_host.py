from pathlib import Path

import httpx
import pytest

from apipi.config import ConfigError, Settings
from apipi.gateway.errors import ApiError
from apipi.worker.pi.model_host import (
    PI_PROVIDER,
    fetch_model_ids,
    fetch_models_json,
    listed_models,
    models_url,
    parse_model_ids,
    probe_model_host,
    require_listed_model,
    require_model,
    write_pi_models_json,
)
from apipi.worker.pi.proc import pi_command_args, pi_env
from apipi.worker.pi.version import PINNED_PI


def _settings(
    tmp_path: Path,
    *,
    model_base_url: str | None = "http://model.test/v1",
    model_api_key_overwrite: str | None = None,
) -> Settings:
    return Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        model_base_url=model_base_url,
        model_api_key_overwrite=model_api_key_overwrite,
    )


def test_models_url_joins() -> None:
    assert models_url("http://host/v1") == "http://host/v1/models"
    assert models_url("http://host/v1/") == "http://host/v1/models"


def test_parse_model_ids() -> None:
    assert parse_model_ids({"data": [{"id": "a"}, {"id": "b"}]}) == ["a", "b"]
    assert parse_model_ids({"data": []}) == []
    assert parse_model_ids({}) == []


def test_require_model() -> None:
    assert require_model(" gpt ") == "gpt"
    with pytest.raises(ApiError) as exc:
        require_model(None)
    assert exc.value.code == "model_required"


def test_require_listed_model() -> None:
    require_listed_model("a", ["a", "b"])
    with pytest.raises(ApiError) as exc:
        require_listed_model("missing", ["a"])
    assert exc.value.code == "model_not_found"


def test_write_pi_models_json(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    path = write_pi_models_json(settings, ["Qwen/Qwen3.6-35B-A3B"])
    text = path.read_text()
    assert PI_PROVIDER in text
    assert "openai-completions" in text
    assert "Qwen/Qwen3.6-35B-A3B" in text
    assert "http://model.test/v1" in text


def test_pi_command_args_include_provider_and_model(tmp_path: Path) -> None:
    args = pi_command_args(_settings(tmp_path), tools=True, model="m1")
    assert args[args.index("--provider") + 1] == PI_PROVIDER
    assert args[args.index("--model") + 1] == "m1"
    assert "--no-session" in args


def test_pi_command_args_session_file(tmp_path: Path) -> None:
    args = pi_command_args(
        _settings(tmp_path), tools=True, session_file=".apipi/pi-session.jsonl"
    )
    assert args[args.index("--session") + 1] == ".apipi/pi-session.jsonl"
    assert "--no-session" not in args


def test_pi_command_args_append_system_prompt(tmp_path: Path) -> None:
    args = pi_command_args(_settings(tmp_path), tools=True, instructions="be brief")
    assert args[args.index("--append-system-prompt") + 1] == "be brief"
    assert "--system-prompt" not in args


def test_pi_command_args_omit_empty_instructions(tmp_path: Path) -> None:
    args = pi_command_args(_settings(tmp_path), tools=True, instructions="")
    assert "--append-system-prompt" not in args
    assert "--system-prompt" not in args
    args = pi_command_args(_settings(tmp_path), tools=True)
    assert "--append-system-prompt" not in args


def test_pi_command_args_include_extension(tmp_path: Path) -> None:
    args = pi_command_args(
        _settings(tmp_path),
        tools=True,
        extension="/workspace/.pi/agent/extensions/apipi-mcp.ts",
    )
    assert args[args.index("--extension") + 1] == (
        "/workspace/.pi/agent/extensions/apipi-mcp.ts"
    )


def test_pi_env_uses_request_key_not_openai_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "from-process")
    env = pi_env(_settings(tmp_path), api_key="from-request")
    assert env["OPENAI_API_KEY"] == "from-request"
    assert env["OPENAI_BASE_URL"] == "http://model.test/v1"
    assert "PI_CODING_AGENT_DIR" in env


def test_pi_env_overwrite(tmp_path: Path) -> None:
    settings = _settings(tmp_path, model_api_key_overwrite="over")
    env = pi_env(settings)
    assert env["OPENAI_API_KEY"] == "over"


def test_fetch_model_ids_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_get(url: str, **_kwargs: object) -> httpx.Response:
        assert url.endswith("/models")
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    monkeypatch.setattr("apipi.worker.pi.model_host.httpx.get", fake_get)
    assert fetch_model_ids("http://model.test/v1", "k") == ["m1"]


def test_listed_models_unauthorized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "apipi.worker.pi.model_host.httpx.get",
        lambda *_args, **_kwargs: httpx.Response(401, json={"error": "no"}),
    )
    with pytest.raises(ApiError) as exc:
        listed_models("http://model.test/v1", "k")
    assert exc.value.code == "model_host_unauthorized"


def test_fetch_models_json_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"object": "list", "data": [{"id": "m1", "object": "model"}]}

    def fake_get(url: str, **kwargs: object) -> httpx.Response:
        assert url.endswith("/models")
        headers = kwargs.get("headers")
        assert isinstance(headers, dict)
        assert headers["Authorization"] == "Bearer k"
        return httpx.Response(200, json=payload)

    monkeypatch.setattr("apipi.worker.pi.model_host.httpx.get", fake_get)
    assert fetch_models_json("http://model.test/v1", "k") == payload


def test_probe_model_host_requires_base_url(tmp_path: Path) -> None:
    settings = _settings(tmp_path, model_base_url=None)
    with pytest.raises(ConfigError, match="OPENAI_BASE_URL is required"):
        probe_model_host(settings)


def test_probe_model_host_requires_pi(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("apipi.worker.pi.model_host.shutil.which", lambda _name: None)
    with pytest.raises(ConfigError, match="pi is not on PATH"):
        probe_model_host(_settings(tmp_path))


def test_probe_model_host_writes_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "apipi.worker.pi.model_host.shutil.which", lambda _name: "/bin/pi"
    )
    monkeypatch.setattr(
        "apipi.worker.pi.model_host.subprocess.check_output",
        lambda *_args, **_kwargs: PINNED_PI + "\n",
    )
    monkeypatch.setattr(
        "apipi.worker.pi.model_host.fetch_model_ids",
        lambda *_args, **_kwargs: ["m1"],
    )
    settings = _settings(tmp_path)
    probe_model_host(settings)
    assert (tmp_path / "sessions" / ".pi" / "agent" / "models.json").is_file()
