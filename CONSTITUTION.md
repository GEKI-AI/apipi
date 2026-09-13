# ApiPi constitution

Rules for the project. Specs say what we build. ADRs say why. Change this
file rarely. Edit in place. Do not keep an amendment log.

## Laws

1. **The gateway is the product. Pi is a worker.**
   Public HTTP types never leak Pi types. Replacing Pi does not change the API.

2. **Postgres is the source of truth.**
   The event log is the transcript. Pi's files are a cache.

3. **The computer is replaceable.**
   none, local directory, or `self_hosted`. Independent of run mode
   (`host` / `jail` / `microvm`). Search and browser are MCP, not built in.
   OpenAI's field `openai_hosted` is a local session directory, not OpenAI's
   cloud. `hosted` is the same local directory.

4. **Pi is the harness we ship.**
   Small loop, RPC. A second adapter later must not change the public API.

5. **We do not mint or store tenant API keys.**
   Browsers do not hold them.
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
