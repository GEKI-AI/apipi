const MCP_TIMEOUT_MS = 120_000;

function parseSse(text, id) {
  const messages = [];
  for (const block of text.split(/\n\n/)) {
    const data = [];
    for (const line of block.split("\n")) {
      if (line.startsWith("data:")) {
        data.push(line.slice(5).trim());
      }
    }
    if (data.length === 0) {
      continue;
    }
    const raw = data.join("\n");
    if (!raw || raw === "[DONE]") {
      continue;
    }
    messages.push(JSON.parse(raw));
  }
  const matched = messages.find((msg) => msg.id === id);
  return matched || messages[messages.length - 1];
}

export class HttpMcpClient {
  constructor(url) {
    this.url = url;
    this.nextId = 1;
    this.sessionId = null;
  }

  request(method, params, timeoutMs = MCP_TIMEOUT_MS) {
    const id = this.nextId++;
    return this.send({ jsonrpc: "2.0", id, method, params }, timeoutMs);
  }

  notify(method, params) {
    return this.send({ jsonrpc: "2.0", method, params }, MCP_TIMEOUT_MS);
  }

  async send(msg, timeoutMs) {
    const headers = {
      Accept: "application/json, text/event-stream",
      "Content-Type": "application/json",
    };
    if (this.sessionId) {
      headers["Mcp-Session-Id"] = this.sessionId;
    }
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    let response;
    try {
      response = await fetch(this.url, {
        method: "POST",
        headers,
        body: JSON.stringify(msg),
        signal: controller.signal,
      });
    } catch (err) {
      if (err && err.name === "AbortError") {
        throw new Error(`mcp timeout ${msg.method}`);
      }
      throw new Error(err instanceof Error ? err.message : String(err));
    } finally {
      clearTimeout(timer);
    }
    const session = response.headers.get("mcp-session-id");
    if (session) {
      this.sessionId = session;
    }
    if (!response.ok) {
      throw new Error(`mcp http ${response.status}`);
    }
    if (msg.id == null) {
      await response.text();
      return null;
    }
    const type = response.headers.get("content-type") || "";
    const text = await response.text();
    if (!text) {
      throw new Error(`mcp ${msg.method} empty response`);
    }
    const body = type.includes("text/event-stream")
      ? parseSse(text, msg.id)
      : JSON.parse(text);
    if (!body || typeof body !== "object") {
      throw new Error(`mcp ${msg.method} bad response`);
    }
    if (body.error) {
      throw new Error(body.error.message || "mcp error");
    }
    return body.result;
  }
}
