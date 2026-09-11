# 0002. Pi is the harness

Pi (`pi --mode rpc`) is the harness we ship.

We chose it because it is small (four tools, short prompt, skills and
MCP as add-ons) and used in production. The public API does not expose
Pi types. Replacing it later would be an adapter, not a new API.

A second harness is not in this version. If we add one, it plugs in
behind the same HTTP surface.
