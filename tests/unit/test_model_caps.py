import base64
import json
from pathlib import Path

import pytest

from apipi.common.errors import ApiError
from apipi.config import Settings, load_settings
from apipi.gateway.content import parse_user_content
from apipi.worker.pi.model_host import PI_PROVIDER, write_pi_models_json


def test_registry_row_marks_image_input(tmp_path: Path) -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
        sessions_dir=str(tmp_path / "sessions"),
        model_base_url="http://model.test/v1",
        model_registry={
            "qwen-vl": {
                "input": ["text", "image"],
                "reasoning": True,
                "context_window": 8192,
                "max_tokens": 1024,
                "thinking_levels": {"low": "low", "high": None},
                "compat": {"thinkingFormat": "qwen"},
            }
        },
    )
    path = write_pi_models_json(settings, ["other"])
    models = json.loads(path.read_text())["providers"][PI_PROVIDER]["models"]
    by_id = {row["id"]: row for row in models}
    assert by_id["other"] == {"id": "other"}
    vision = by_id["qwen-vl"]
    assert vision["input"] == ["text", "image"]
    assert vision["reasoning"] is True
    assert vision["contextWindow"] == 8192
    assert vision["maxTokens"] == 1024
    assert vision["thinkingLevelMap"]["high"] is None
    assert vision["compat"]["thinkingFormat"] == "qwen"


def test_toml_models_table_is_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "apipi.toml"
    path.write_text('[models."qwen-vl"]\ninput = ["text", "image"]\nreasoning = true\n')
    monkeypatch.chdir(tmp_path)
    loaded = load_settings(config_path=str(path))
    assert loaded.model_registry["qwen-vl"]["input"] == ["text", "image"]
    assert loaded.model_registry["qwen-vl"]["reasoning"] is True


def test_input_image_rejects_remote_url() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    with pytest.raises(ApiError, match="data URLs only"):
        parse_user_content(
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": "https://example.com/a.png"}
                ],
            },
            settings=settings,
        )


def test_input_keeps_later_text_parts() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    png = base64.b64encode(
        base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
        )
    ).decode()
    parsed = parse_user_content(
        [
            {
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "first"},
                    {
                        "type": "input_image",
                        "image_url": f"data:image/png;base64,{png}",
                    },
                    {"type": "input_text", "text": "second"},
                ],
            }
        ],
        settings=settings,
    )
    assert parsed.text == "first\nsecond"
    assert len(parsed.images) == 1
    ref = {"type": "image", "file_id": "file-1"}
    parts = parsed.wire_parts([ref])
    assert parts[0]["text"] == "first"
    assert parts[1] == ref
    assert parts[2]["text"] == "second"


def test_input_image_takes_file_id_or_image_url() -> None:
    settings = Settings(
        database_url="postgresql+asyncpg://apipi:apipi@localhost:5432/apipi",
        run_mode="none",
    )
    parsed = parse_user_content(
        {
            "role": "user",
            "content": [
                {"type": "input_image", "file_id": "file-abc", "detail": "low"}
            ],
        },
        settings=settings,
    )
    assert parsed.images[0].file_id == "file-abc"
    assert parsed.images[0].data == b""
    for part in (
        {"type": "input_image"},
        {"type": "input_image", "file_id": "file-abc", "image_url": "data:x"},
    ):
        with pytest.raises(ApiError, match="image_url or file_id"):
            parse_user_content({"role": "user", "content": [part]}, settings=settings)
