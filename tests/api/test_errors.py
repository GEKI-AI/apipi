import logging
from collections.abc import AsyncIterator

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from apipi.gateway.errors import ApiError, register_exception_handlers
from apipi.gateway.schemas import StrictModel


class Probe(StrictModel):
    name: str


def _app() -> FastAPI:
    app = FastAPI()
    register_exception_handlers(app)

    @app.post("/probe")
    def probe(body: Probe) -> dict[str, str]:
        return {"name": body.name}

    @app.get("/fail")
    def fail() -> None:
        raise ApiError("not_implemented", "nope", code="later", status_code=501)

    @app.get("/boom")
    def boom() -> None:
        raise RuntimeError("hidden")

    return app


@pytest.fixture
async def client() -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=_app()), base_url="http://test"
    ) as client:
        yield client


async def test_unknown_field(client: AsyncClient) -> None:
    response = await client.post("/probe", json={"name": "a", "extra": True})
    assert response.status_code == 400
    assert response.json() == {
        "error": {
            "type": "invalid_request",
            "code": "unknown_field",
            "message": "Unknown field: extra",
        }
    }


async def test_not_implemented(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="apipi")
    response = await client.get("/fail")
    assert response.status_code == 501
    assert response.json() == {
        "error": {"type": "not_implemented", "code": "later", "message": "nope"}
    }
    errors = [
        record
        for record in caplog.records
        if record.name == "apipi" and record.__dict__.get("event") == "api.error"
    ]
    assert errors
    assert errors[-1].__dict__["error_code"] == "later"


async def test_unexpected_is_internal(
    client: AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.ERROR, logger="apipi")
    with pytest.raises(RuntimeError, match="hidden"):
        await client.get("/boom")
    errors = [
        record
        for record in caplog.records
        if record.name == "apipi" and record.__dict__.get("event") == "api.error"
    ]
    assert errors
    assert errors[-1].__dict__["error_code"] == "internal"


def test_strict_model_rejects_extra() -> None:
    with pytest.raises(ValidationError):
        Probe.model_validate({"name": "a", "nope": 1})
