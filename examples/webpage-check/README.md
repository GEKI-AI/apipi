# Webpage-check extension

An ApiPi-based FastAPI service that keeps the Agents API and adds
`POST /examples/webpage-check`. The handler creates a session
in-process (no HTTP loopback), asks a preconfigured agent to fetch the
page with `curl` over bash, and streams short plain-text lines
(`using bash…`, then the summary). It does not use Playwright.

Run `apipi migrate` against the same SQLite file first. You need Pi on
`PATH` and a model host for a live fetch.

```
export OPENAI_BASE_URL=http://your-model-host/v1
export APIPI_EXAMPLE_MODEL=your-model
export DATABASE_URL=sqlite+aiosqlite:///.apipi/webpage-check.db
uv run apipi migrate
uv run uvicorn --app-dir examples/webpage-check --factory app:create_example_app \
  --host 0.0.0.0 --port 8000
```

The example reads `APIPI_EXAMPLE_DATABASE_URL` if you set it; otherwise it
uses `.apipi/webpage-check.db` under the process working directory. Point
`DATABASE_URL` at that same file when you migrate.

From another shell:

```
curl -N -X POST http://localhost:8000/examples/webpage-check \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com"}'
```

`GET /ok` is on the thinner skeleton `examples/extend_fastapi.py`.
How to extend ApiPi is in [docs/extending.md](../../docs/extending.md).
The Agents API is still at `/v1` on this process.
