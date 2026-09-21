# 0011. Host credential broker

The model API key and HTTP MCP credentials stay on the host. Pi and
the guest see only a rewrite of `OPENAI_BASE_URL` and MCP URLs that
point at a small reverse proxy next to that session.

The proxy is in-process Python (asyncio, httpx, Starlette). It is not
MITM, not `HTTPS_PROXY`, and not a shared `0.0.0.0` open proxy. A
microVM binds the TAP host IP so only that guest can reach it. Isolation
`none` binds loopback with a per-session path token.

The model upstream is the operator `OPENAI_BASE_URL`. The proxy strips
guest `Authorization` and injects the gateway model key
(`OPENAI_API_KEY_OVERWRITE` or the request bearer). HTTP MCP upstreams
are the registered tool URLs. The proxy injects a matching vault
bearer, or host-expanded tool headers, and does not put those values
in guest env.

Stdio MCP is out of scope. Arbitrary guest `curl` that ignores the
rewritten URLs is a later MITM story, not this path.

Vaults follow the OpenAI Agents shape (`static_bearer` first). They
are tenant-scoped store rows, not the gateway auth bearer. Vault
tokens are encrypted at rest with AES-256-GCM
(`APIPI_VAULT_MASTER_KEY`, ciphertext prefix `v1:`).
