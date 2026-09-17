import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";

const DEFAULT_BASH_TIMEOUT_SEC = 120;
const MCP_TIMEOUT_MS = 120_000;
const INSTALL_RE =
  /\b(?:npm|pnpm|yarn|bun)\s+(?:install|i|add)\b[\s\S]*\bplaywright\b|\b(?:npx\s+)?playwright\s+install\b|\bnpm\s+exec\s+playwright\s+install\b/i;

type JsonRpc = {
  jsonrpc?: string;
  id?: number;
  method?: string;
  params?: unknown;
  result?: unknown;
  error?: { message?: string };
};

function sanitize(raw: string): string {
  const cleaned = raw.replace(/[^a-zA-Z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  return cleaned || "tool";
}

function encode(msg: object): Buffer {
  const json = JSON.stringify(msg);
  return Buffer.from(`Content-Length: ${Buffer.byteLength(json)}\r\n\r\n${json}`);
}

class McpClient {
  private buf = Buffer.alloc(0);
  private nextId = 1;
  private readonly pending = new Map<
    number,
    { resolve: (value: unknown) => void; reject: (err: Error) => void }
  >();

  constructor(private readonly proc: ChildProcessWithoutNullStreams) {
    proc.stdout.on("data", (chunk: Buffer) => {
      this.buf = Buffer.concat([this.buf, chunk]);
      this.drain();
    });
    proc.stderr.on("data", () => {});
    proc.on("exit", () => {
      for (const [, waiter] of this.pending) {
        waiter.reject(new Error("mcp process exited"));
      }
      this.pending.clear();
    });
  }

  private drain(): void {
    while (true) {
      const msg = this.readOne();
      if (msg === null) {
        return;
      }
      if (msg.id == null) {
        continue;
      }
      const waiter = this.pending.get(msg.id);
      if (waiter === undefined) {
        continue;
      }
      this.pending.delete(msg.id);
      if (msg.error) {
        waiter.reject(new Error(msg.error.message || "mcp error"));
      } else {
        waiter.resolve(msg.result);
      }
    }
  }

  private readOne(): JsonRpc | null {
    const headerEnd = this.buf.indexOf("\r\n\r\n");
    if (headerEnd >= 0) {
      const header = this.buf.subarray(0, headerEnd).toString("utf8");
      const match = /Content-Length:\s*(\d+)/i.exec(header);
      if (match) {
        const len = Number(match[1]);
        const start = headerEnd + 4;
        if (this.buf.length < start + len) {
          return null;
        }
        const json = this.buf.subarray(start, start + len).toString("utf8");
        this.buf = this.buf.subarray(start + len);
        return JSON.parse(json) as JsonRpc;
      }
    }
    const nl = this.buf.indexOf(0x0a);
    if (nl < 0) {
      return null;
    }
    const line = this.buf.subarray(0, nl).toString("utf8").trim();
    this.buf = this.buf.subarray(nl + 1);
    if (!line.startsWith("{")) {
      return this.readOne();
    }
    return JSON.parse(line) as JsonRpc;
  }

  request(method: string, params?: unknown): Promise<unknown> {
    const id = this.nextId++;
    const msg = { jsonrpc: "2.0", id, method, params };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`mcp timeout ${method}`));
        }
      }, MCP_TIMEOUT_MS);
      this.pending.set(id, {
        resolve: (value) => {
          clearTimeout(timer);
          resolve(value);
        },
        reject: (err) => {
          clearTimeout(timer);
          reject(err);
        },
      });
      this.proc.stdin.write(encode(msg));
    });
  }

  notify(method: string, params?: unknown): void {
    this.proc.stdin.write(encode({ jsonrpc: "2.0", method, params }));
  }
}

function schemaOf(inputSchema: unknown) {
  if (inputSchema && typeof inputSchema === "object") {
    return Type.Unsafe(inputSchema);
  }
  return Type.Object({}, { additionalProperties: true });
}

function contentOf(result: unknown): { content: { type: "text"; text: string }[]; isError?: boolean } {
  if (!result || typeof result !== "object") {
    return { content: [{ type: "text", text: JSON.stringify(result ?? "") }] };
  }
  const raw = result as {
    isError?: boolean;
    content?: Array<{ type?: string; text?: string; data?: string; mimeType?: string }>;
  };
  const parts: { type: "text"; text: string }[] = [];
  for (const item of raw.content ?? []) {
    if (item.type === "text" && typeof item.text === "string") {
      parts.push({ type: "text", text: item.text });
      continue;
    }
    if (item.type === "image" && typeof item.data === "string") {
      parts.push({
        type: "text",
        text: `image ${item.mimeType ?? "image/png"} (${item.data.length} bytes base64)`,
      });
      continue;
    }
    parts.push({ type: "text", text: JSON.stringify(item) });
  }
  if (parts.length === 0) {
    parts.push({ type: "text", text: JSON.stringify(result) });
  }
  return { content: parts, isError: raw.isError };
}

function stdioServers(): Array<{ label: string; command: string; args: string[]; cwd?: string }> {
  const labels = process.env.APIPI_MCP_STDIO;
  if (!labels) {
    return [];
  }
  const servers = [];
  for (const [index, raw] of labels.split(",").entries()) {
    const prefix = `APIPI_MCP_STDIO_${index}`;
    const command = process.env[`${prefix}_COMMAND`];
    if (!command) {
      continue;
    }
    const packed = process.env[`${prefix}_ARGS`] ?? "";
    const args = packed ? packed.split("\x1f") : [];
    const cwd = process.env[`${prefix}_CWD`] || undefined;
    servers.push({ label: raw.trim() || command, command, args, cwd });
  }
  return servers;
}

function startServer(server: {
  label: string;
  command: string;
  args: string[];
  cwd?: string;
}): Promise<ChildProcessWithoutNullStreams> {
  return new Promise((resolve, reject) => {
    const proc = spawn(server.command, server.args, {
      cwd: server.cwd,
      env: { ...process.env, PLAYWRIGHT_CHROMIUM_SANDBOX: "0" },
      stdio: ["pipe", "pipe", "pipe"],
    }) as ChildProcessWithoutNullStreams;
    const fail = (err: Error) => {
      proc.removeListener("error", onError);
      proc.removeListener("exit", onExit);
      if (proc.exitCode === null) {
        proc.kill();
      }
      reject(err);
    };
    const onError = (err: Error) => fail(err);
    const onExit = () => fail(new Error(`mcp ${server.label} failed`));
    proc.once("error", onError);
    proc.once("exit", onExit);
    setTimeout(() => {
      proc.removeListener("error", onError);
      proc.removeListener("exit", onExit);
      if (proc.exitCode !== null) {
        reject(new Error(`mcp ${server.label} failed`));
        return;
      }
      resolve(proc);
    }, 50);
  });
}

async function attachStdio(pi: ExtensionAPI): Promise<void> {
  for (const server of stdioServers()) {
    const proc = await startServer(server);
    const client = new McpClient(proc);
    await client.request("initialize", {
      protocolVersion: "2024-11-05",
      capabilities: {},
      clientInfo: { name: "apipi", version: "0.2.0" },
    });
    client.notify("notifications/initialized");
    const listed = (await client.request("tools/list", {})) as {
      tools?: Array<{ name: string; description?: string; inputSchema?: unknown }>;
    };
    for (const tool of listed.tools ?? []) {
      const name = `mcp_${sanitize(server.label)}_${sanitize(tool.name)}`;
      pi.registerTool({
        name,
        label: `${server.label} ${tool.name}`,
        description: tool.description || `${server.label} MCP tool ${tool.name}`,
        promptSnippet: tool.description || `${server.label} ${tool.name}`,
        promptGuidelines: [
          `Use ${name} for ${server.label} MCP (${tool.name}). Do not reimplement it with bash.`,
        ],
        parameters: schemaOf(tool.inputSchema),
        async execute(_toolCallId, params, signal) {
          if (signal?.aborted) {
            return { content: [{ type: "text", text: "aborted" }], isError: true };
          }
          const result = await client.request("tools/call", {
            name: tool.name,
            arguments: params,
          });
          return contentOf(result);
        },
      });
    }
  }
}

export default function (pi: ExtensionAPI) {
  pi.on("tool_call", (event) => {
    if (event.toolName !== "bash") {
      return;
    }
    const input = event.input as { command?: string; timeout?: number };
    const command = input.command ?? "";
    if (INSTALL_RE.test(command)) {
      return {
        block: true,
        reason:
          "Do not install Playwright or browser binaries. Use the Playwright MCP tools and system Chromium.",
      };
    }
    if (input.timeout === undefined) {
      input.timeout = DEFAULT_BASH_TIMEOUT_SEC;
    }
    return;
  });

  pi.on("session_start", async () => {
    try {
      await attachStdio(pi);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(message);
      process.exit(1);
    }
  });
}
