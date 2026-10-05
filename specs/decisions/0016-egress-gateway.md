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

The guest has no IPv6. The worker sets `disable_ipv6` on the TAP before
the link comes up, so the host side never gets a link-local address,
and `ip6tables` drops every IPv6 packet from the TAP in `INPUT` and
`FORWARD`. Without this a guest could reach host services on `[::]`
through the link-local address, and a host that forwards IPv6 would
forward guest packets unfiltered. The worker needs `ip6tables` like
`iptables`; on a kernel without IPv6 there is nothing to close.

Each gateway resolves hostnames with the worker's system resolver on
its own small thread pool, at most 8 lookups at a time and 5 seconds
per lookup. A blocking `getaddrinfo` keeps its thread after the timeout
frees the caller, so a pool shared by all sessions would let one guest
that asks for names whose servers never answer stop the lookups of
every other session.

## Hostname policy

The gateway decides by hostname, not by IP address:

* On 443 and 8443 it reads the SNI from the TLS ClientHello without
  consuming it. On 80 it reads the `Host` header.
* `restricted` (session `allowed_domains`, or the operator TAP
  allowlist) allows only listed hostnames. A connection without SNI, or
  with an IP literal as SNI or `Host`, is rejected. A ClientHello with
  two `server_name` extensions, or with more than one name in the list,
  is not read as TLS and is rejected. Other TCP ports and other UDP are
  rejected. Guest DNS goes to a small resolver on the TAP host IP that
  answers only allowed names and returns `NXDOMAIN` for the rest, and
  forwards only a query it builds again from the name and type, so DNS
  cannot carry data out.
* `enabled` allows public hosts, IP literals included, as today. Other
  ports keep the direct NAT path, and guest DNS goes directly to the
  public resolvers.
* `disabled` blocks all guest egress, DNS included. The guest reaches
  only its broker on the TAP host IP. No gateway is started.
* Private and special-use ranges stay rejected in every mode. In
  `restricted`, and for every host whose TLS the gateway terminates, the
  gateway resolves the hostname itself, checks every address, and
  connects to an address it resolved, so DNS rebinding and a changed
  `/etc/hosts` in the guest do not matter. In `enabled` the gateway
  splices to the address the guest connected to after checking that it
  is not in a blocked range, so `curl --resolve` and `/etc/hosts` keep
  working for public hosts.
* The only exception to the private ranges is an operator list of
  private hosts (`private_hosts`) that the gateway, never the guest, may
  reach, for example a self-hosted Forgejo. A listed name opens a
  private address only when the session allows that name, or when the
  gateway terminates its TLS. The list is per worker, not per tenant.
  The guest cannot resolve an internal name with public resolvers, so
  the DNS resolver of a `restricted` guest answers an allowed listed
  name with the placeholder `198.18.0.1` (no records for other query
  types). The guest's connection to the placeholder on 80, 443, or 8443
  is sent to the gateway like any other, and the gateway resolves the
  name from SNI or `Host` on the worker. The guest never learns the
  internal address.

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
`SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`, `GIT_SSL_CAINFO`,
`NODE_EXTRA_CA_CERTS`, and `CURL_CA_BUNDLE` at it.

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
like `static_bearer` tokens and is never returned. It must be 8 to
16,384 characters of printable ASCII without spaces, so it fits a
header value unchanged and masking cannot replace short common strings.

The guest environment sets `secret_name` to a random placeholder that
is unique per sandbox start and credential. The gateway replaces the
placeholder with `secret_value` only on intercepted HTTPS requests
(ports 443 and 8443) to `allowed_hosts`:

* in request header values, including `Authorization: Basic` after
  base64 decoding, so `git` over HTTPS works,
* never in the request line (path and query string),
* never in the body.

The query string is excluded because many services accept form fields
there and store or render them (an issue title, a search term). If the
gateway substituted the query string, the guest could make a credential
host write the real secret into a page and read it back.

A request to any other host carries only the placeholder. Credential
hosts are HTTPS only: plain HTTP on port 80 to such a host is rejected
(`credential_host_plain_http`). On requests to a credential's hosts the
gateway removes `Upgrade` and `Connection`, so nothing is passed through
unmasked after a `101`, removes `Range` and `If-Range`, so the secret
cannot be split across partial responses, asks for `Accept-Encoding:
identity`, and masks the response headers (also of `1xx` responses) and
body with one longest-first pass, dropping response trailers. The mask
strings are the exact secret, its JSON string forms (with and without
`\/`), its percent-encoded forms (including the `encodeURIComponent`
form), the Basic token the gateway built for the request itself
(taken from that exchange, so a shared cache can never drop it while the
response is in flight), and the Basic tokens it built for earlier
requests to that host (a bounded cache), each replaced with the token
the guest sent, longest first. Other transformations (hashes, partial
copies, base64 of the bare secret built by the agent) are not masked.

Body masking streams: each piece of a read is decoded, masked, and sent
before the next is produced, so a connection holds at most one decode
chunk and a carry. The carry is only the longest suffix that is a proper
prefix of a mask string, so streaming responses are not delayed.
Because the length changes, the gateway drops `Content-Length` and sends
the body chunked. A `gzip` body (also several members) or `deflate`
body (zlib or raw) is decompressed in bounded steps; a truncated stream
aborts the connection before the final chunk, and a body that expands
past 10 MiB and 100 times its compressed size is aborted
(`egress.decode_limit`). Any other content encoding is a `502`.

This is defense in depth, not a boundary: the guest chooses which header
carries the placeholder, so a credential host that stores a request
header and shows it somewhere other than in the response could still
expose the secret, and with `enabled` the guest can reach the same
server by IP address or another server name, which the gateway splices
unchanged. Tokens and `allowed_hosts` must be scoped to what the task
needs.

Request signing (AWS SigV4, HMAC), keys that only work in the query
string, and non-HTTP protocols cannot work this way. Function tools
cover those.

A small git credential helper, written by the worker onto the
workspace drive and configured through `GIT_CONFIG_COUNT` in the guest
environment, answers with the placeholder for hosts in a credential's
`allowed_hosts` (port 443 and 8443), and `url.<https>.insteadOf` rewrites
SSH remotes for those hosts to HTTPS. So
`git clone https://host/org/repo` and `git clone git@host:org/repo`
work without a token in the URL, and no guest image change is needed.

The gateway parses each intercepted request with `h11`, so a later
per-host rule (for example, which git refs may be pushed) is a hook on
a parsed request. No such rules exist now. A credential has exactly the
rights of its token.

## Session rules

* Environment credentials on a session that does not run in a microVM
  are `400`. MCP credentials in the same vault keep working there.
  The API cannot know a worker's isolation at session create, because
  `apipi serve` has no run mode. It refuses to send a command with
  environment credentials to a worker that does not run `microvm`
  (`400`, `credential_not_allowed`), and to a worker that does not list
  the protocol feature `env_credentials` (`501`). A worker without an
  egress gateway fails the sandbox start with the same code.
* Environment credentials with `network.access` `disabled` are `400`.
* With `restricted`, the `allowed_hosts` of the session's credentials
  are added to the session's allowed hostnames.
* With the operator TAP allowlist on, a credential host must be allowed
  by the operator, or session create is `400`. A tenant cannot open a
  host by creating a credential.
* Two attached credentials with the same `secret_name` are `400`.
  Reserved names are rejected when the credential is written: the
  prefixes `OPENAI_`, `APIPI_`, `PI_`, `CODEX_`, `GIT_`, and
  `AGENT_BROWSER_`, the guest environment deny list, and the variables
  guest init sets (`PATH`, `HOME`, `USER`, `SHELL`, `PWD`, the CA bundle
  variables, the npm and uv cache variables, `WS`, `CA_BUNDLE`, `CA_DIR`,
  and `cmd`).

## When secrets are read

All vault credentials, MCP and environment, are a snapshot taken when
the sandbox starts. A change takes effect when the sandbox starts
again (idle stop, crash, respawn, or another worker). This matches
OpenAI. The API still sends the resolved credentials in the context of
every command, so the API stays stateless and any replica can start the
sandbox with current values.

## Logs

The gateway logs and counts each connection with host, port, decision
(spliced, intercepted, rejected), and for injections the credential id
and host. Worker logs never include a secret value or a placeholder:
the command context is not logged, its repr leaves the values out, and
the gateway logs only ids and hosts. The payload export redacts the
secret values of the session's vaults.
