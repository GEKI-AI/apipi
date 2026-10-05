from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from tests.support.files import message

from apipi.api.sessions import SessionEventBody
from apipi.gateway.errors import register_exception_handlers

_TEXT = {"type": "input_text", "text": "x"}


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/events")
    def events(body: SessionEventBody) -> dict[str, str]:
        return {"shape": type(body).__name__}

    return app


@pytest.mark.parametrize(
    ("body", "code", "text"),
    [
        ({**message(_TEXT), "foo": 1}, "unknown_field", "Unknown field: foo"),
        (
            {"type": "agent.session.input.cancel", "foo": 1},
            "unknown_field",
            "Unknown field: foo",
        ),
        (
            {**message(_TEXT), "type": "agent.session.input.message"},
            "unknown_field",
            "Unknown field: type",
        ),
        (message({"type": "input_text"}), "validation_error", "Invalid request"),
        ({"events": []}, "validation_error", "Invalid request"),
        ({"content": "x"}, "validation_error", "Invalid request"),
    ],
)
async def test_event_body_errors_belong_to_the_shape_sent(
    body: dict[str, Any], code: str, text: str
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        response = await client.post("/events", json=body)
    assert response.status_code == 400
    error = response.json()["error"]
    assert (error["code"], error["message"]) == (code, text)
