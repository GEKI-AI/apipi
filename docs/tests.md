# Tests

Pytest lives in `tests/`. Product pages say what is true. Tests check
the public API and the constitution, not Pi internals. Mock Pi RPC
except in the live e2e files. The same change as the code.

Local pytest uses SQLite in memory. Optional Postgres:

```
export APIPI_TEST_DATABASE_URL=postgresql+asyncpg://apipi:apipi@localhost:5432/apipi
```

## Commands

From a checkout after `uv sync`:

| What | Command |
| --- | --- |
| Same as GitHub Tests | `uv run pytest -n auto -m "not slow"` |
| Everything, including slow | `uv run pytest -n auto` |
| Unit only | `uv run pytest tests/unit` |
| Public HTTP only | `uv run pytest tests/api` |
| Fast e2e (none, microvm) | `uv run pytest -m e2e` |
| None e2e | `uv run pytest tests/e2e/test_none_pi.py` |
| Microvm e2e | `uv run pytest -m microvm` |
| Slow only | `uv run pytest -m slow` |
| Lint, types, and tests | `./scripts/check` |
| Like GitHub (skip slow) | `./scripts/check --fast` |
| Also build the docs site | `./scripts/check --docs` |
| Manual live examples (microvm API + worker) | `./examples/sessions/run-microvm.sh` |

If a live suite cannot start, those tests skip. The suite still
requires the mode it asked for.

## Suites

| Path | Marker | What | GitHub |
| --- | --- | --- | --- |
| `tests/unit/` | none | Internals with mocks: config, store, isolation contract, microvm image packing, artifacts | yes |
| `tests/api/` | none | Public HTTP vs [api.md](api.md). Every API test runs the API app (`create_app`, which is always the API) through an in-process worker (`split_client_for`, `tests/support/split_worker.py`) that connects over the real worker socket and runs FakeHarness. The API and the worker talk only over that socket, the same as in production. There is no combined test mode. Tenant isolation. `test_compat.py` has one named test per yes row on the API page. `test_worker_accepts.py` is the worker accepts placement and `type=none` tool-policy matrix | yes |
| `tests/e2e/test_none_pi.py` | `e2e` | Real `apipi serve` and `apipi worker` subprocesses (`tests/support/procs.py`, ephemeral loopback port, tmp dirs) with a fake Pi in `none` mode | yes |
| `tests/e2e/test_microvm_pi.py` | `e2e`, `microvm` | Same two-process shape with a microvm worker, inside a real Firecracker guest | no (skips without KVM) |
| `tests/e2e/test_metrics_scrape.py` | `e2e` | `/metrics` scrape from the same two-process setup | yes |
| `tests/e2e/test_pi_live.py` | `slow` | `pi` is on `PATH` | no |
| `tests/e2e/test_openai_sdk.py` | `slow` | Official OpenAI Python client `beta.agents` against the same two-process setup | no |
| `tests/unit/test_worker_protocol_schema.py`, `tests/api/test_conformance_api.py`, `tests/unit/test_conformance_worker.py` | none | The worker protocol contract: the committed JSON Schema matches the models and every frame the API and the worker send validates against it, and the real API and the real worker each play the golden transcripts in `tests/fixtures/worker-protocol/` (see [worker protocol](worker-protocol.md#json-schema-and-golden-transcripts)) | yes |
| `tests/support/` | — | FakeHarness helpers, fake Pi, fake worker, and the transcript player (`conformance.py`). Not a suite | — |

GitHub runs `pytest -n auto -m "not slow"` (pytest-xdist; drop `-n` to debug one test or when `APIPI_TEST_DATABASE_URL` is set). That is unit, API, and `e2e`.
Microvm tests skip if KVM, Firecracker, images, or net tools cannot
start. GitHub CI stays without Firecracker.

`./scripts/check` runs the slow tests too. They skip when `pi` or the
OpenAI SDK is missing.

## Timeouts

Every test has a limit of 120 seconds. The limit comes from
[pytest-timeout](https://github.com/pytest-dev/pytest-timeout) and is
set as `timeout` in `[tool.pytest.ini_options]` in `pyproject.toml`. It
counts the test together with the setup and teardown of its fixtures.
The slowest normal tests take a few seconds, so the limit only trips on
a test that hangs. The GitHub jobs also have `timeout-minutes` in
`.github/workflows/ci.yml` (20 minutes for `Tests`, 10 for the others),
in case the whole run hangs.

The timeout method is `thread`. When a test runs past its limit,
pytest-timeout prints the stack of every thread and ends the process.
Under pytest-xdist (`-n`) that process is one worker. The log then
says `worker 'gw2' crashed while running 'tests/...::test_name'`, the
test counts as failed, xdist starts a new worker, and the other tests
still run. The `signal` method is not used: it raises the failure
inside the test, but when a fixture teardown then waits on the same
stuck thing (for example an async server fixture), the run still
hangs. Without `-n`, the first timeout ends the whole pytest run; add
`-v` so the name of the running test is printed before the stacks.

pytest-timeout stops the timer when a test fails, so that a debugger
can be used on the failure. Then a teardown that hangs after the
failure, for example a task that does not end when the event loop
of the test is closed, would have no limit. `tests/conftest.py` starts
the timer again after a failure in setup or in the test, with the
full limit, unless pytest runs with `--pdb`. A hang in the teardown of
a failed test then also ends with its name.

A test that needs longer sets its own limit in seconds with a mark:

```python
@pytest.mark.timeout(300)
async def test_something_slow() -> None:
    ...
```

A module sets it for all of its tests with
`pytestmark = [pytest.mark.timeout(900)]`. For one run, pass
`--timeout=600` to `uv run pytest`, or `--timeout=0` to turn the limit
off, for example while you debug one test. `-p no:timeout` turns off
pytest-timeout completely; the restart after a failure is then skipped
too.

These tests have their own limit:

| Test | Limit | Why |
| --- | --- | --- |
| `tests/e2e/test_microvm_pi.py` | 300 s | Boots Firecracker guests and waits up to 90 s for the API and the worker to start |
| `tests/e2e/test_microvm_egress.py` | 900 s | Waits up to 10 minutes for the probe inside the guest, which reaches hosts on the internet |

## None e2e

Needs Python 3.13. Uses `tests/support/fake_pi.py` as `APIPI_PI_COMMAND`.
No KVM.

```
uv run pytest tests/e2e/test_none_pi.py
```

## Microvm e2e

These boot a real Firecracker guest. GitHub does not run them. They
skip unless every requirement below is present.

Need:

| Need | Check |
| --- | --- |
| `/dev/kvm` readable and writable | member of group `kvm`; `ls -l /dev/kvm` |
| `firecracker` and `jailer` on `PATH` | `firecracker --version` |
| `ip`, `iptables`, `ip6tables`, and `tc` | `command -v ip iptables ip6tables tc` |
| Guest kernel | From the image store (`apipi images pull`). `APIPI_MICROVM_KERNEL` is a dev-only override (a `vmlinux` file). |
| Guest rootfs | From the image store (`apipi images pull <id>`). `APIPI_MICROVM_ROOTFS` is a dev-only override (ext4 with Node, Pi, `python3` or `socat`, and `/sbin/apipi-guest`). |
| TAP | Permission to create a TAP device (`CAP_NET_ADMIN` or root) |
| IP forward | `/proc/sys/net/ipv4/ip_forward` is `1`, or you can write it |

Your user must be in group `kvm` (or otherwise able to open
`/dev/kvm`):

```
python3 -c "import os; print(os.access('/dev/kvm', os.R_OK | os.W_OK))"
```

That must print `True`. TAP creation is the usual extra step after KVM
works.
`apipi install --microvm` downloads Firecracker and jailer and pulls
guest images from the image store. You can still install Firecracker from the
[Firecracker release](https://github.com/firecracker-microvm/firecracker/releases)
and build images with `apipi images build`. The distro stays out
of git. Pull images before running microvm tests:

```
uv run apipi install --microvm
uv run pytest -m microvm
```

`apipi images build <id>` needs `curl`, `tar`, `mkfs.ext4`, `mount`, and
root (or `sudo`) for the loop mount. How to install
Firecracker and what the rootfs must contain are in
[run modes](run-modes.md).

If kernel, rootfs, KVM, or TAP cannot start, the tests skip. They still
require a real microVM.

`tests/e2e/test_microvm_egress.py` checks the egress gateway from inside
a guest: allowed and other hosts, IP addresses, other ports, DNS,
private hosts by name through the DNS placeholder, the CA bundle with `curl`, Python, Node, and `git`, a
`git clone` from a private credential host in an `enabled` guest, and
that a `disabled` guest reaches only the broker.
It needs internet access from the worker host and root, because it
also runs a private upstream on `127.0.0.1:443`. The hosts default to
`example.com`, `example.org`, `github.com`, and `localtest.me` (a
public name for `127.0.0.1`). Override them with
`APIPI_E2E_EGRESS_HOST`, `APIPI_E2E_EGRESS_OTHER_HOST`,
`APIPI_E2E_EGRESS_INTERCEPT_HOST`, `APIPI_E2E_EGRESS_GIT_REPO`, and
`APIPI_E2E_EGRESS_PRIVATE_HOST`.

## Slow tests

```
uv run pytest -m slow
```

`test_pi_live.py` skips unless `pi` is on `PATH`. `test_openai_sdk.py`
skips unless the `openai` package with `beta.agents` is installed. It
does not call a real model:

```
uv run --with openai pytest tests/e2e/test_openai_sdk.py
```

The runnable client script against a live gateway is
`examples/sessions/openai_sdk.py`; see [Using the API](using.md).

## Manual microvm examples

This is a live run against a real model host. It is not pytest. GitHub
CI does not run it. `./scripts/check` does not run it.

The suite starts one `apipi serve` process and one
`apipi worker` with `APIPI_RUN_MODE=microvm`, then runs every script in
`examples/sessions/` (write-and-run `tree.py`, inject-and-sort a file,
size `L` browser screenshot). The playground and
Session examples are separate; they are not in this
script.

### Requirements

| Need | What |
| --- | --- |
| Checkout | `uv sync` already done |
| Model host | `OPENAI_BASE_URL` in `.env` or the environment is the **model** URL Pi calls, not the gateway |
| Model key | `OPENAI_API_KEY_OVERWRITE` in `.env` if you want one operator key; otherwise the client bearer is passed through to the model host |
| Model id | `APIPI_MODEL` must be an id from that host (`GET /v1/models` once the API is up) |
| Client bearer | `APIPI_EXAMPLE_TOKEN` or default `dev-token`. Default auth accepts any non-empty bearer |
| Postgres | Compose `postgres` service, or `DATABASE_URL` pointing at a migrated database |
| KVM | `/dev/kvm` readable and writable; Firecracker, jailer, `ip`, `iptables`, `ip6tables`, `tc` |
| Images | `uv run apipi install --microvm --image browser` so both rootfs files exist |
| sudo | Passwordless sudo for TAP and jailer on the worker |
| Port 8000 | Free. The script exits if something already listens there |

Copy `examples/env.example` to `.env` at the repo root and set the
model host. Do not put the worker token or model key in the browser or
in git.

```
# .env (gateway and worker)
OPENAI_BASE_URL=https://your-model-host/v1
# OPENAI_API_KEY_OVERWRITE=...
```

```
export APIPI_MODEL=your-model-id
# optional:
# export APIPI_EXAMPLE_TOKEN=dev-token
# (the script mints a per-worker token itself; see run-microvm.sh)
# export DATABASE_URL=postgresql+asyncpg://apipi:apipi@127.0.0.1:5432/apipi
./examples/sessions/run-microvm.sh
```

The script migrates Postgres, starts the API on `0.0.0.0:8000`, starts
the worker, waits for `GET /health` and a worker `hello`, then runs
`examples/sessions/run.sh`. Logs and downloaded artifacts go to
`/tmp/opencode/apipi-examples` unless you set `APIPI_EXAMPLES_OUT`.
On this machine the API is also reachable at
`http://192.168.0.49:8000`. Ctrl-C stops the API and worker the
script started.

If the API is already running, skip `run-microvm.sh` and point the
clients at it:

```
export OPENAI_API_KEY=dev-token
export OPENAI_BASE_URL=http://127.0.0.1:8000/v1
export APIPI_MODEL=your-model-id
./examples/sessions/run.sh
```

On the **scripts**, `OPENAI_BASE_URL` is the ApiPi gateway. On the
**gateway process**, `OPENAI_BASE_URL` is the model host. Do not mix
those two meanings in the same shell without resetting the variable.
