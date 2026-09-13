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
| Fast e2e (host, jail, microvm) | `uv run pytest -m e2e` |
| Host e2e | `uv run pytest tests/e2e/test_host_pi.py` |
| Jail e2e | `uv run pytest -m jail` |
| Microvm e2e | `uv run pytest -m microvm` |
| Slow only | `uv run pytest -m slow` |
| Lint, types, and tests | `./scripts/check` |
| Like GitHub (skip slow) | `./scripts/check --fast` |
| Also build the docs site | `./scripts/check --docs` |

If a live suite cannot start, those tests skip. That skip is not a
fallback to another run mode.

## Suites

| Path | Marker | What | GitHub |
| --- | --- | --- | --- |
| `tests/unit/` | none | Internals with mocks: config, store, jail argv, microvm image packing, artifacts | yes |
| `tests/api/` | none | Public HTTP vs [api.md](api.md). FakeHarness. Tenant isolation. `test_compat.py` has one named test per yes row on the API page | yes |
| `tests/e2e/test_host_pi.py` | `e2e` | Live session against a fake Pi process in `host` mode | yes |
| `tests/e2e/test_jail_pi.py` | `e2e`, `jail` | Same shape inside a real bubblewrap jail | yes, if jail tools start |
| `tests/e2e/test_microvm_pi.py` | `e2e`, `microvm` | Same shape inside a real Firecracker guest | no (skips without KVM) |
| `tests/e2e/test_metrics_scrape.py` | `e2e` | `/metrics` scrape | yes |
| `tests/e2e/test_pi_live.py` | `slow` | `pi` is on `PATH` | no |
| `tests/e2e/test_openai_sdk.py` | `slow` | Official OpenAI Python client `beta.agents` against FakeHarness | no |
| `tests/support/` | — | FakeHarness helpers, fake Pi, fake runner. Not a suite | — |

GitHub runs `pytest -m "not slow"`. That is unit, API, and `e2e`.
Jail tests skip if `bwrap`, `pasta`, or cgroup v2 cannot start. Microvm
tests skip if KVM, Firecracker, images, or net tools cannot start. Do
not add Firecracker to GitHub.

`./scripts/check` runs the slow tests too. They skip when `pi` or the
OpenAI SDK is missing.

## Host e2e

Needs Python 3.13. Uses `tests/support/fake_pi.py` as `APIPI_PI_COMMAND`.
No jail tools and no KVM.

```
uv run pytest tests/e2e/test_host_pi.py
```

## Jail e2e

Needs Linux, cgroup v2, unprivileged user namespaces, `bwrap`, and
`pasta` (`passt` package). CI installs those. On a laptop:

```
# Debian / Ubuntu
sudo apt-get install -y bubblewrap passt

uv run pytest -m jail
```

The service user must be able to create a child cgroup. See
[run modes](run-modes.md). If jail cannot start, the tests skip.

## Microvm e2e

These boot a real Firecracker guest. GitHub does not run them. They
skip unless every requirement below is present.

Need:

| Need | Check |
| --- | --- |
| `/dev/kvm` readable and writable | member of group `kvm`; `ls -l /dev/kvm` |
| `firecracker` and `jailer` on `PATH` | `firecracker --version` |
| `ip` and `iptables` | `command -v ip iptables` |
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
Install Firecracker from the
[Firecracker release](https://github.com/firecracker-microvm/firecracker/releases).
Do not vendor a distro in git. Build images on the machine:

```
./scripts/microvm-rootfs
export APIPI_MICROVM_KERNEL="$HOME/.cache/apipi/microvm/vmlinux"
export APIPI_MICROVM_ROOTFS="$HOME/.cache/apipi/microvm/rootfs.ext4"
uv run pytest -m microvm
```

`./scripts/microvm-rootfs` needs `curl`, `tar`, `mkfs.ext4`, `mount`,
and root (or `sudo`) for the loop mount. Pass a directory argument to
write the images somewhere else. How to install Firecracker and what
the rootfs must contain are in [run modes](run-modes.md).

If kernel, rootfs, KVM, or TAP cannot start, the tests skip. That is
not a fallback to `jail` or `host`.

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
`examples/openai_sdk.py`; see [Using the API](using.md).
