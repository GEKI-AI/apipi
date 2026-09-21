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
| Same as GitHub Tests | `uv run pytest -m "not slow"` |
| Everything, including slow | `uv run pytest` |
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
| `tests/api/` | none | Public HTTP vs [api.md](api.md). FakeHarness. Tenant isolation. `test_compat.py` has one named test per yes row on the API page. `test_chat_path_a.py` is the Chat Path A placement, facade, and tool-policy matrix | yes |
| `tests/e2e/test_none_pi.py` | `e2e` | Live session against a fake Pi process in `none` mode | yes |
| `tests/e2e/test_microvm_pi.py` | `e2e`, `microvm` | Same shape inside a real Firecracker guest | no (skips without KVM) |
| `tests/e2e/test_metrics_scrape.py` | `e2e` | `/metrics` scrape | yes |
| `tests/e2e/test_pi_live.py` | `slow` | `pi` is on `PATH` | no |
| `tests/e2e/test_openai_sdk.py` | `slow` | Official OpenAI Python client `beta.agents` against FakeHarness | no |
| `tests/support/` | — | FakeHarness helpers, fake Pi, fake runner. Not a suite | — |

GitHub runs `pytest -m "not slow"`. That is unit, API, and `e2e`.
Microvm tests skip if KVM, Firecracker, images, or net tools cannot
start. GitHub CI stays without Firecracker.

`./scripts/check` runs the slow tests too. They skip when `pi` or the
OpenAI SDK is missing.

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
| `ip`, `iptables`, and `tc` | `command -v ip iptables tc` |
| Guest kernel | `APIPI_MICROVM_KERNEL` (a `vmlinux` file) |
| Guest rootfs | `APIPI_MICROVM_ROOTFS` (ext4 with Node, Pi, `python3` or `socat`, and `/sbin/apipi-guest`) |
| TAP | Permission to create a TAP device (`CAP_NET_ADMIN` or root) |
| IP forward | `/proc/sys/net/ipv4/ip_forward` is `1`, or you can write it |

Your user must be in group `kvm` (or otherwise able to open
`/dev/kvm`):

```
python3 -c "import os; print(os.access('/dev/kvm', os.R_OK | os.W_OK))"
```

That must print `True`. TAP creation is the usual extra step after KVM
works.
`apipi install --microvm` downloads Firecracker and jailer and builds
guest images. You can still install Firecracker from the
[Firecracker release](https://github.com/firecracker-microvm/firecracker/releases)
and build images with `./scripts/microvm-rootfs`. The distro stays out
of git. Unset `APIPI_MICROVM_KERNEL` and `APIPI_MICROVM_ROOTFS` use the
cache files when they exist:

```
uv run apipi install --microvm
uv run pytest -m microvm
```

```
./scripts/microvm-rootfs
uv run pytest -m microvm
```

`./scripts/microvm-rootfs` needs `curl`, `tar`, `mkfs.ext4`, `mount`,
and root (or `sudo`) for the loop mount. Pass a directory argument to
write the images somewhere else. `--flavor browser` writes
`rootfs-browser.ext4` next to the default image. How to install
Firecracker and what the rootfs must contain are in
[run modes](run-modes.md).

If kernel, rootfs, KVM, or TAP cannot start, the tests skip. They still
require a real microVM.

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

The suite starts one `apipi serve --api-only` process and one
`apipi worker` with `APIPI_RUN_MODE=microvm`, then runs every script in
`examples/sessions/` (write-and-run `tree.py`, inject-and-sort a file,
size `L` browser screenshot). The playground and
`examples/self_hosted_runner.py` are separate; they are not in this
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
| KVM | `/dev/kvm` readable and writable; Firecracker, jailer, `ip`, `iptables`, `tc` |
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
# export APIPI_WORKER_TOKEN=local-worker
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
