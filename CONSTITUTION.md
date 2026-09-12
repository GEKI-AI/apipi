# ApiPi constitution

Rules for the project. Specs say what we build. ADRs say why. Change this
file rarely, and only with a dated amendment at the bottom.

## Laws

1. **The gateway is the product. Pi is a worker.**
   Public HTTP types never leak Pi types. Replacing Pi does not change the API.

2. **Postgres is the source of truth.**
   The event log is the transcript. Pi's files are a cache.

3. **The computer is replaceable.**
   none, local directory, or `self_hosted`. Independent of run mode
   (`host` / `jail` / `microvm`). Search and browser are MCP, not built in.

4. **Pi is the harness we ship.**
   Small loop, RPC. A second adapter later must not change the public API.

5. **We do not mint or store tenant API keys.**
   Browsers do not hold them. The example UI uses a demo cookie.
   Production clients send a bearer the gateway does not keep.
   Auth is a callback; default hashes the key (`docs/auth.md`).

6. **Compatible where it helps, honest where it does not.**
   Official OpenAI clients should work for the subset we implement. Unknown
   fields and missing features fail clearly.

7. **Data stays where you put it.**
   Self-host and a hosted deploy are the same code. Residency is a
   deploy choice.

8. **Thin on purpose.**
   If it can be an MCP server or a worker, it does not go in the gateway.

9. **Specs lead. Code follows.**
   Code that contradicts a spec is a bug. Wrong specs are updated in the
   same change as the code. When a decision changes, edit the ADR.

10. **A session export is enough to leave.**
    The customer keeps the thread if we disappear.

## What this is

ApiPi is a drop-in OpenAI Agents API. Point official clients at this
gateway and bring your own model URL.

The package and CLI are `apipi`. Hosted at geki.ai.

## What this is not

- A workflow builder
- A model host
- A search engine
- A copy of every OpenAI Agents object

## Amendments

### 2026-09-11

The product is the API. The chat is only an example.

`openai_hosted` is a local directory, not OpenAI's cloud.

Run mode is `host` | `jail` | `microvm`. Default `jail`. Independent of
environment.

Pi is the harness we ship. Skills are `SKILL.md` on the computer.

### 2026-09-12

We do not mint or store tenant API keys. Auth is a callback. Default
accepts any bearer and hashes it for `key_id` / `tenant_id`.
