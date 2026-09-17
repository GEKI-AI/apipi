# Session scripts

These Python scripts create an ApiPi session, stream the first turn, and
exit. They are clients. They are not the playground, and they are not
pytest. Keys come from the environment, not from these files.

| File | What |
| --- | --- |
| [openai_sdk.py](openai_sdk.py) | Official OpenAI Python client: write `tree.py`, run it |
| [transform_file.py](transform_file.py) | Inject `fruits.txt`, sort it, download `outputs/fruits-sorted.txt` |
| [browser_screenshot.py](browser_screenshot.py) | Size `L`: open a page, screenshot under `outputs/` |

A gateway must already be running unless you use
[run-microvm.sh](run-microvm.sh). On the **client**, `OPENAI_BASE_URL`
is this ApiPi process (`http://localhost:8000/v1`), not the model host.
`OPENAI_API_KEY` is the bearer the gateway will accept. `APIPI_MODEL`
must exist on the model host that the gateway process uses.

```
export OPENAI_API_KEY=dev-token
export OPENAI_BASE_URL=http://localhost:8000/v1
export APIPI_MODEL=your-model-id
uv run --with openai python examples/sessions/openai_sdk.py
uv run python examples/sessions/transform_file.py
uv run --with openai python examples/sessions/browser_screenshot.py
```

`transform_file.py` uses `httpx` from the ApiPi checkout, so it does
not need `--with openai`. `APIPI_OUTPUT` sets the local filename for
the downloaded artifact. `APIPI_PAGE_URL` sets the page for the
browser script (default `https://example.com`).

`browser_screenshot.py` needs isolation `microvm` and the browser
rootfs. The other two scripts work on size `S` in `none` or `microvm`.

To start a split API plus worker in `microvm` and run every session
script, see [Manual microvm examples](../../docs/tests.md#manual-microvm-examples)
and [run-microvm.sh](run-microvm.sh).
