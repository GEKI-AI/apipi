from prometheus_client import CONTENT_TYPE_LATEST
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from apipi.gateway.metrics import Metrics


def metrics_app(metrics: Metrics) -> Starlette:
    async def scrape(_request: Request) -> Response:
        return Response(content=metrics.scrape(), media_type=CONTENT_TYPE_LATEST)

    async def health(_request: Request) -> PlainTextResponse:
        return PlainTextResponse("ok")

    return Starlette(routes=[Route("/metrics", scrape), Route("/health", health)])


async def serve_metrics(metrics: Metrics, *, host: str, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(
        metrics_app(metrics),
        host=host,
        port=port,
        log_config=None,
        access_log=False,
    )
    await uvicorn.Server(config).serve()
