# 0005. The API is the focus

The product is `/v1`. Clients, SDKs, and other apps talk to that.

The chat at `/_example/` is only an example. Off by default. On with
`APIPI_EXAMPLE_UI=1`. Demo cookie on that path only. Real clients use
bearer tokens and their own UI.
