# 0016. Egress gateway for microVM guests

A microVM guest reaches the outside world through an egress gateway
on the worker host. The gateway enforces the network policy by
hostname and injects vault secrets into HTTPS requests, so code in the
guest can call APIs with a key it never holds. This extends the host
credential broker (0011) from the rewritten model and MCP URLs to
ordinary guest traffic such as `curl`, `git`, CLIs, and SDKs.

Isolation `none` is out of scope. Its process runs on the worker host,
so a proxy cannot keep a secret from it, and it already cannot enforce
`environment.network`.

## Placement

The gateway is Python asyncio code in the worker process, next to the
broker, in its own package `apipi.worker.egress`. There is one
gateway listener per session, on the TAP host IP, with the same
lifecycle as that session's broker. It holds only the secrets of its
own session. No other language and no extra daemon are added.

The worker sends guest TCP to the gateway with iptables `DNAT` on the
guest TAP for ports 80, 443, and 8443. The guest needs no proxy
variables, and tools that ignore `HTTPS_PROXY` are covered. The
gateway reads the original destination with `SO_ORIGINAL_DST`.

## Hostname policy

The gateway decides by hostname, not by IP address:

* On 443 and 8443 it reads the SNI from the TLS ClientHello without
  consuming it. On 80 it reads the `Host` header.
* `restricted` (session `allowed_domains`, or the operator TAP
  allowlist) allows only listed hostnames. A connection without SNI, or
  with an IP literal as SNI or `Host`, is rejected. Other TCP ports and
  other UDP are rejected. Guest DNS goes to a small resolver on the TAP
  host IP that answers only allowed names and returns `NXDOMAIN` for the
  rest, so DNS cannot carry data out.
* `enabled` allows public hosts, IP literals included, as today. Other
  ports keep the direct NAT path.
* `disabled` keeps blocking all guest egress.
* Private and special-use ranges stay rejected in every mode. The
  gateway resolves the hostname itself and checks every address, so DNS
  rebinding and a changed `/etc/hosts` in the guest do not matter. The
  only exception is an operator list of private hosts that the gateway
  (never the guest) may reach, for example a self-hosted Forgejo.

This replaces the current allowlist, which resolves hostnames to IP
addresses once at boot and opens those addresses on every port. That
breaks when a CDN rotates addresses, and it opens every site that
shares an address with an allowed host.

UDP 443 is rejected so that QUIC clients fall back to TCP.

## TLS interception

The gateway terminates TLS only for hosts that a vault credential of
the session names. Every other allowed connection is spliced through
unchanged, so certificate pinning to other hosts keeps working.

Each worker creates a certificate authority when it starts. The key
stays in worker memory and is never written to disk or sent to a guest.
A worker restart ends all its guests (see workers), so no guest
outlives the authority it trusts, and nothing needs to be stored or
rotated. Leaf certificates are made on demand per hostname and cached
in the worker.

The guest gets only the authority certificate, on the workspace drive.
Guest init writes a combined bundle of the image's system authorities
and the worker authority, and the guest environment points
`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO`, and
`NODE_EXTRA_CA_CERTS` at it.

On an intercepted connection the gateway:

* offers only `http/1.1` in ALPN,
* requires `Host` to match the SNI,
* connects upstream to the hostname it resolved itself and verifies
  the real certificate against the system authorities, plus an optional
  operator bundle for private upstreams.

HTTP/2, gRPC, and non-HTTP protocols are not intercepted.

## Environment variable credentials

A vault credential of type `environment_variable` follows the OpenAI
Agents shape: `secret_name`, `secret_value`, and `networking`
(`type: limited`, `allowed_hosts`). `secret_value` is encrypted at rest
like `static_bearer` tokens and is never returned.

The guest environment sets `secret_name` to a random placeholder that
is unique per session and credential. The gateway replaces the
placeholder with `secret_value` only on intercepted HTTPS requests to
`allowed_hosts`:

* in request header values, including `Authorization: Basic` after
  base64 decoding, so `git` over HTTPS works,
* in the request query string,
* never in the body.

A request to any other host carries only the placeholder. The gateway
replaces the secret with the placeholder again in response headers.
Request signing (AWS SigV4, HMAC) and non-HTTP protocols cannot work
this way. Function tools cover those.

A small git credential helper in the guest image answers with the
placeholder for hosts in a credential's `allowed_hosts`, so
`git clone https://host/org/repo` works without a token in the URL.

The gateway parses each intercepted request with `h11`, so a later
per-host rule (for example, which git refs may be pushed) is a hook on
a parsed request. No such rules exist now. A credential has exactly the
rights of its token.

## Session rules

* Environment credentials on a session that does not run in a microVM
  are `400`. MCP credentials in the same vault keep working there.
* Environment credentials with `network.access` `disabled` are `400`.
* With `restricted`, the `allowed_hosts` of the session's credentials
  are added to the session's allowed hostnames.
* With the operator TAP allowlist on, a credential host must be allowed
  by the operator, or session create is `400`. A tenant cannot open a
  host by creating a credential.
* Two attached credentials with the same `secret_name` are `400`.
  Reserved names (`OPENAI_*`, `APIPI_*`, `PI_*`, `PATH`, and the guest
  environment deny list) are rejected when the credential is written.

## When secrets are read

All vault credentials, MCP and environment, are a snapshot taken when
the sandbox starts. A change takes effect when the sandbox starts
again (idle stop, crash, respawn, or another worker). This matches
OpenAI. The API still sends the resolved credentials in the context of
every command, so the API stays stateless and any replica can start the
sandbox with current values.

## Logs

The gateway logs and counts each connection with host, port, decision
(spliced, intercepted, rejected), and for injections the credential id.
It never logs a secret value or a placeholder. Secret values join the
redaction list of the worker and of the payload export.
