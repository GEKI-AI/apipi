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
    proc.stderr.on("data", (chunk: Buffer) => {
      const text = chunk.toString("utf8").trim();
      if (text) {
        console.error(`mcp: ${text.slice(0, 500)}`);
      }
    });
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

  request(method: string, params?: unknown, timeoutMs = MCP_TIMEOUT_MS): Promise<unknown> {
    const id = this.nextId++;
    const msg = { jsonrpc: "2.0", id, method, params };
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        if (this.pending.has(id)) {
          this.pending.delete(id);
          reject(new Error(`mcp timeout ${method}`));
        }
      }, timeoutMs);
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

function waitForSpawn(proc: ChildProcessWithoutNullStreams): Promise<void> {
  return new Promise((resolve, reject) => {
    let last = Date.now();
    let saw = false;
    let done = false;
    const finish = (err?: Error) => {
      if (done) {
        return;
      }
      done = true;
      clearInterval(timer);
      proc.stderr.off("data", onErr);
      proc.stdout.off("data", onErr);
      if (err) {
        reject(err);
        return;
      }
      resolve();
    };
    const onErr = (chunk: Buffer) => {
      if (chunk.length) {
        saw = true;
        last = Date.now();
      }
    };
    proc.stderr.on("data", onErr);
    proc.stdout.on("data", onErr);
    const timer = setInterval(() => {
      if (proc.exitCode !== null) {
        finish(new Error("mcp process exited"));
        return;
      }
      const idle = Date.now() - last;
      if ((saw && idle >= 2000) || idle >= 20000) {
        finish();
      }
    }, 200);
    setTimeout(() => finish(), MCP_TIMEOUT_MS);
  });
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
      env: {
        ...process.env,
        PLAYWRIGHT_CHROMIUM_SANDBOX: "0",
        PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1",
        npm_config_yes: "true",
        CI: "true",
        NPM_CONFIG_LOGLEVEL: "error",
        npm_config_progress: "false",
        npm_config_fund: "false",
      },
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

function warmupPackage(pkg: string): Promise<void> {
  return new Promise((resolve, reject) => {
    const proc = spawn("npx", ["-y", `--package=${pkg}`, "node", "-e", "process.exit(0)"], {
      env: {
        ...process.env,
        npm_config_yes: "true",
        PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1",
        NPM_CONFIG_LOGLEVEL: "error",
      },
      stdio: ["ignore", "pipe", "pipe"],
    });
    const fail = (err: Error) => {
      proc.kill();
      reject(err);
    };
    proc.stderr.on("data", (chunk: Buffer) => {
      const text = chunk.toString("utf8").trim();
      if (text) {
        console.error(`mcp warmup: ${text.slice(0, 500)}`);
      }
    });
    proc.once("error", (err) => fail(err instanceof Error ? err : new Error(String(err))));
    proc.once("exit", (code) => {
      if (code === 0) {
        resolve();
        return;
      }
      fail(new Error(`npx warmup failed (${code})`));
    });
    setTimeout(() => fail(new Error("npx warmup timeout")), MCP_TIMEOUT_MS);
  });
}

async function attachStdio(pi: ExtensionAPI): Promise<void> {
  for (const server of stdioServers()) {
    const pkg = server.args.find((item) => item.includes("mcp") || item.startsWith("@"));
    if (server.command === "npx" && pkg) {
      await warmupPackage(pkg);
    }
    const proc = await startServer(server);
    await waitForSpawn(proc);
    const client = new McpClient(proc);
    const deadline = Date.now() + MCP_TIMEOUT_MS;
    let last = new Error(`mcp ${server.label} initialize failed`);
    while (Date.now() < deadline) {
      try {
        await client.request(
          "initialize",
          {
            protocolVersion: "2024-11-05",
            capabilities: {},
            clientInfo: { name: "apipi", version: "0.2.0" },
          },
          8000,
        );
        last = new Error("");
        break;
      } catch (err) {
        last = err instanceof Error ? err : new Error(String(err));
      }
    }
    if (last.message) {
      proc.kill();
      throw last;
    }
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
      throw err;
    }
  });
}
