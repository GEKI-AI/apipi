"""Socket-level shared-root check (#448)."""

from typing import Any, cast

from httpx import ASGITransport, AsyncClient
from tests.support.fake_worker import FakeWorker
from tests.support.split_worker import api_settings_for

from apipi.config import Settings
from apipi.gateway import create_app
from apipi.store.engine import Store


async def test_wrong_store_proof_closes_socket(
    settings: Settings, store: Store, worker_secret: str
) -> None:
    app = create_app(api_settings_for(settings), store=store)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        transport = client._transport
        assert isinstance(transport, ASGITransport)
        real_app = cast(Any, transport.app)
        worker = FakeWorker(real_app, worker_secret)
        hello = await worker.connect()
        assert hello.get("store_check") is not None
        await worker.send_json(
            {"type": "store.proof", "marker": "nope", "nonce": "wrong"}
        )
        closed = await worker.wait_close(timeout=5)
        assert closed.get("code") == 1008
