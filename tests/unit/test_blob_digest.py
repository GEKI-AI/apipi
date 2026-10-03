import asyncio
import hashlib
import io
from typing import Any

from apipi.common.objects import NS_ARTIFACTS
from apipi.config import Settings
from apipi.store.blobs import HASH_CHUNK, LocalStore, MemoryStore, S3Store, hash_stream


def test_hash_stream_reads_in_chunks() -> None:
    data = b"a" * (HASH_CHUNK * 2 + 17)
    sizes: list[int] = []
    stream = io.BytesIO(data)

    def read(amount: int) -> bytes:
        chunk = stream.read(amount)
        sizes.append(len(chunk))
        return chunk

    assert hash_stream(read) == (len(data), hashlib.sha256(data).hexdigest())
    assert max(sizes) == HASH_CHUNK


async def test_stores_digest_without_loading_the_object(settings: Settings) -> None:
    data = b"hello world"
    expected = (len(data), hashlib.sha256(data).hexdigest())
    memory = MemoryStore()
    await memory.put(NS_ARTIFACTS, "t/k/s/a", data)
    assert await memory.digest(NS_ARTIFACTS, "t/k/s/a") == expected
    assert await memory.digest(NS_ARTIFACTS, "t/k/s/missing") is None
    local = LocalStore(settings)
    await local.put(NS_ARTIFACTS, "t/k/s/a", data)
    assert await local.digest(NS_ARTIFACTS, "t/k/s/a") == expected
    assert await local.digest(NS_ARTIFACTS, "t/k/s/missing") is None


async def test_s3_digest_streams_the_body_off_the_event_loop(
    settings: Settings,
) -> None:
    data = b"s3 body" * 1000

    class Body(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            self.main_thread_free = True
            return super().read(size)

    class Client:
        def get_object(self, **kwargs: Any) -> dict[str, Any]:
            return {"Body": Body(data)}

    store = S3Store(
        settings.model_copy(update={"s3_bucket": "bucket"}), client=Client()
    )
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0)
            ticks += 1

    task = asyncio.create_task(tick())
    try:
        result = await store.digest(NS_ARTIFACTS, "t/k/s/a")
    finally:
        task.cancel()
    assert result == (len(data), hashlib.sha256(data).hexdigest())
    assert ticks > 0
