# 0014. OpenAI Agents API compatibility

ApiPi stays compatible with the OpenAI Agents API
(`client.beta.agents.*`, `OpenAI-Beta: agents=v1`). Where a shape
exists upstream, ApiPi uses it: same routes, fields, event names, and
values.

Where compatibility is impossible, the extra behavior lives under
`/v1/apipi/...`. Do not bend the compatible route. The compatible
route and the extension route call the same service. HTTP only
validates and serializes. `session_body`, `event_body`, and the turn,
item, and artifact body functions are the public response shapes.

Extension fields are additive and grouped under one key per object
when they are new (`environment.sandbox`). Existing flat extras
(`idle_ttl`, `user_id`, `org_id`, `sandbox_size`, `sandbox_image`)
stay flat so current clients do not break. Stock SDK inputs keep
`metadata["apipi.<key>"]`.

Unknown request keys are `unknown_field`. Known but unsupported OpenAI
fields are `not_implemented`. Responses and event `data` may carry
extra keys.

ApiPi does not mirror Responses, Assistants, Conversations, or Chat
Completions. ApiPi-only routes that used to sit in the OpenAI
namespace keep working as deprecated aliases for at least one minor
release. The canonical paths are under `/v1/apipi/`.
