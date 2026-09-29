import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { McpClient } from "./mcp_client.mjs";
import { HttpMcpClient } from "./mcp_http.mjs";

const DEFAULT_BASH_TIMEOUT_SEC = 120;
const ATTACH_TIMEOUT_MS = 15_000;
const INSTALL_RE =
  /\b(?:npm|pnpm|yarn|bun)\s+(?:install|i|add)\b[\s\S]*\bplaywright\b|\b(?:npx\s+)?playwright\s+install\b|\bnpm\s+exec\s+playwright\s+install\b/i;
const CHECK_URL = "data:text/html,<title>apipi</title><p>ok</p>";

let playwrightTools = false;

type StdioServer = {
  label: string;
  command: string;
  args: string[];
  cwd?: string;
};

type ListedTool = {
  name: string;
  description?: string;
  inputSchema?: unknown;
};

type RpcClient = {
  request(method: string, params?: unknown, timeoutMs?: number): Promise<unknown>;
  notify(method: string, params?: unknown): void | Promise<void>;
};

function sanitize(raw: string): string {
  const cleaned = raw.replace(/[^a-zA-Z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
  return cleaned || "tool";
}

function attachError(label: string, phase: string, err: unknown): Error {
  const message = err instanceof Error ? err.message : String(err);
  return new Error(
    `mcp attach failed server=${label} phase=${phase} error=${message}`,
  );
}

function schemaOf(inputSchema: unknown) {
  if (inputSchema && typeof inputSchema === "object") {
    return Type.Unsafe(inputSchema);
  }
  return Type.Object({}, { additionalProperties: true });
}

function contentOf(result: unknown): {
  content: { type: "text"; text: string }[];
  isError?: boolean;
} {
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

function httpServers(): Array<{ label: string; url: string }> {
  const labels = process.env.APIPI_MCP_SERVERS;
  if (!labels) {
    return [];
  }
  const servers = [];
  for (const [index, raw] of labels.split(",").entries()) {
    const url = process.env[`APIPI_MCP_${index}_URL`];
    if (!url) {
      continue;
    }
    const named = process.env[`APIPI_MCP_${index}_LABEL`];
    servers.push({ label: (named || raw).trim() || "mcp", url });
  }
  return servers;
}

function stdioServers(): StdioServer[] {
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

function startServer(server: StdioServer): Promise<ChildProcessWithoutNullStreams> {
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
        NPM_CONFIG_CACHE: process.env.NPM_CONFIG_CACHE || "/tmp/npm-cache",
        npm_config_cache: process.env.npm_config_cache || "/tmp/npm-cache",
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

function warmupPackage(pkg: string, timeoutMs: number): Promise<void> {
  return new Promise((resolve, reject) => {
    const proc = spawn("npx", ["-y", `--package=${pkg}`, "node", "-e", "process.exit(0)"], {
      env: {
        ...process.env,
        npm_config_yes: "true",
        PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD: "1",
        NPM_CONFIG_LOGLEVEL: "error",
        NPM_CONFIG_CACHE: process.env.NPM_CONFIG_CACHE || "/tmp/npm-cache",
        npm_config_cache: process.env.npm_config_cache || "/tmp/npm-cache",
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
    setTimeout(() => fail(new Error("npx warmup timeout")), timeoutMs);
  });
}

function isPlaywright(server: StdioServer): boolean {
  if (server.label.toLowerCase() === "playwright") {
    return true;
  }
  const blob = [server.command, ...server.args].join(" ");
  return blob.includes("playwright/mcp") || blob.includes("playwright-mcp");
}

function chromiumPath(server: StdioServer): string | null {
  const blob = [server.command, ...server.args].join(" ");
  if (blob.includes("/usr/bin/chromium-browser")) {
    return "/usr/bin/chromium-browser";
  }
  return null;
}

function registerTools(
  pi: ExtensionAPI,
  label: string,
  tools: ListedTool[],
  client: RpcClient,
  opts: { playwright: boolean; chromium: string | null },
): void {
  let first = true;
  for (const tool of tools) {
    const name = `mcp_${sanitize(label)}_${sanitize(tool.name)}`;
    const lines = [
      `Use ${name} for ${label} MCP (${tool.name}). Do not reimplement it with bash.`,
    ];
    if (first && opts.playwright) {
      const where = opts.chromium ? `Chromium is at ${opts.chromium}. ` : "";
      lines.push(
        `${where}Drive the browser only through these MCP tools. Save screenshots under outputs/. Do not npm install playwright or download browsers.`,
      );
    }
    pi.registerTool({
      name,
      label: `${label} ${tool.name}`,
      description: tool.description || `${label} MCP tool ${tool.name}`,
      promptSnippet: tool.description || `${label} ${tool.name}`,
      promptGuidelines: lines,
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
    first = false;
  }
  if (opts.playwright && tools.length > 0) {
    playwrightTools = true;
  }
}

function workspaceRoot(): string {
  return process.env.HOME || "/workspace";
}

function writeCheckReport(report: {
  tools: string[];
  screenshot: string | null;
  error: string | null;
}): void {
  const dir = join(workspaceRoot(), "outputs");
  mkdirSync(dir, { recursive: true });
  writeFileSync(join(dir, "mcp-check.json"), `${JSON.stringify(report)}\n`);
}

async function runPlaywrightCheck(
  client: McpClient,
  server: StdioServer,
  tools: ListedTool[],
): Promise<void> {
  const names = tools.map(
    (tool) => `mcp_${sanitize(server.label)}_${sanitize(tool.name)}`,
  );
  const report = {
    tools: names,
    screenshot: null as string | null,
    error: null as string | null,
  };
  try {
    const nav = tools.find((tool) => tool.name === "browser_navigate");
    const shot = tools.find((tool) => tool.name === "browser_take_screenshot");
    if (!nav || !shot) {
      throw new Error("missing browser_navigate or browser_take_screenshot");
    }
    await client.request("tools/call", {
      name: nav.name,
      arguments: { url: CHECK_URL },
    });
    await client.request("tools/call", {
      name: shot.name,
      arguments: { filename: "check.png" },
    });
    report.screenshot = "outputs/check.png";
  } catch (err) {
    report.error = err instanceof Error ? err.message : String(err);
  }
  writeCheckReport(report);
  if (report.error) {
    throw new Error(report.error);
  }
}

async function attachStdio(
  pi: ExtensionAPI,
): Promise<Array<{ server: StdioServer; client: McpClient; tools: ListedTool[] }>> {
  const deadline = Date.now() + ATTACH_TIMEOUT_MS;
  const remaining = (): number => Math.max(1, deadline - Date.now());
  const playwright: Array<{ server: StdioServer; client: McpClient; tools: ListedTool[] }> =
    [];
  for (const server of stdioServers()) {
    if (Date.now() >= deadline) {
      throw attachError(server.label, "spawn", new Error("mcp attach timeout"));
    }
    const pkg = server.args.find((item) => item.includes("mcp") || item.startsWith("@"));
    if (server.command === "npx" && pkg) {
      try {
        await warmupPackage(pkg, remaining());
      } catch (err) {
        throw attachError(server.label, "spawn", err);
      }
    }
    let proc: ChildProcessWithoutNullStreams;
    try {
      proc = await startServer(server);
    } catch (err) {
      throw attachError(server.label, "spawn", err);
    }
    const client = new McpClient(proc);
    let last: Error = attachError(
      server.label,
      "initialize",
      new Error(`mcp ${server.label} initialize failed`),
    );
    let ready = false;
    while (Date.now() < deadline) {
      try {
        await client.request(
          "initialize",
          {
            protocolVersion: "2024-11-05",
            capabilities: {},
            clientInfo: { name: "apipi", version: "0.2.0" },
          },
          Math.min(8000, remaining()),
        );
        ready = true;
        break;
      } catch (err) {
        last = attachError(server.label, "initialize", err);
      }
    }
    if (!ready) {
      proc.kill();
      throw last;
    }
    client.notify("notifications/initialized");
    let listed: { tools?: ListedTool[] };
    try {
      listed = (await client.request("tools/list", {}, remaining())) as {
        tools?: ListedTool[];
      };
    } catch (err) {
      proc.kill();
      throw attachError(server.label, "tools/list", err);
    }
    const tools = listed.tools ?? [];
    const playwrightServer = isPlaywright(server);
    registerTools(pi, server.label, tools, client, {
      playwright: playwrightServer,
      chromium: chromiumPath(server),
    });
    if (playwrightServer && tools.length > 0) {
      playwright.push({ server, client, tools });
    }
  }
  return playwright;
}

async function attachHttp(pi: ExtensionAPI): Promise<void> {
  const deadline = Date.now() + ATTACH_TIMEOUT_MS;
  const remaining = (): number => Math.max(1, deadline - Date.now());
  for (const server of httpServers()) {
    if (Date.now() >= deadline) {
      throw attachError(server.label, "initialize", new Error("mcp attach timeout"));
    }
    const client = new HttpMcpClient(server.url);
    try {
      await client.request(
        "initialize",
        {
          protocolVersion: "2024-11-05",
          capabilities: {},
          clientInfo: { name: "apipi", version: "0.2.0" },
        },
        Math.min(8000, remaining()),
      );
    } catch (err) {
      throw attachError(server.label, "initialize", err);
    }
    try {
      await client.notify("notifications/initialized");
    } catch (err) {
      void err;
    }
    let listed: { tools?: ListedTool[] };
    try {
      listed = (await client.request("tools/list", {}, remaining())) as {
        tools?: ListedTool[];
      };
    } catch (err) {
      throw attachError(server.label, "tools/list", err);
    }
    registerTools(pi, server.label, listed.tools ?? [], client, {
      playwright: server.label.toLowerCase() === "playwright",
      chromium: null,
    });
  }
}

const PI_INTRO = "You are an expert coding assistant operating inside pi";

function replaceIntro(prompt: string, identity: string): string | null {
  const at = prompt.indexOf(PI_INTRO);
  if (at < 0) {
    return null;
  }
  const end = prompt.indexOf("\n", at);
  const rest = end < 0 ? "" : prompt.slice(end);
  return identity + rest;
}

export default function (pi: ExtensionAPI) {
  pi.on("before_agent_start", (event) => {
    const dir = process.env.PI_CODING_AGENT_DIR;
    if (!dir) {
      return;
    }
    let identity = "";
    try {
      identity = readFileSync(`${dir}/identity.txt`, "utf8").trim();
    } catch {
      return;
    }
    if (!identity) {
      return;
    }
    const current = event.systemPrompt ?? "";
    const next = replaceIntro(current, identity);
    if (next === null) {
      console.error("apipi identity intro was not found; keeping Pi's prompt");
      return;
    }
    return { systemPrompt: next };
  });

  pi.on("tool_call", (event) => {
    if (event.toolName !== "bash") {
      return;
    }
    const input = event.input as { command?: string; timeout?: number };
    const command = input.command ?? "";
    if (playwrightTools && INSTALL_RE.test(command)) {
      return {
        block: true,
        reason:
          "Do not install Playwright or browser binaries. Use the Playwright MCP tools.",
      };
    }
    if (input.timeout === undefined) {
      input.timeout = DEFAULT_BASH_TIMEOUT_SEC;
    }
    return;
  });

  pi.on("session_start", async () => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let playwright: Array<{
      server: StdioServer;
      client: McpClient;
      tools: ListedTool[];
    }> = [];
    try {
      playwright = await Promise.race([
        (async () => {
          let httpError: Error | undefined;
          try {
            await attachHttp(pi);
          } catch (err) {
            httpError = err instanceof Error ? err : new Error(String(err));
          }
          let found: Array<{
            server: StdioServer;
            client: McpClient;
            tools: ListedTool[];
          }> = [];
          let stdioError: Error | undefined;
          try {
            found = await attachStdio(pi);
          } catch (err) {
            stdioError = err instanceof Error ? err : new Error(String(err));
          }
          if (httpError) {
            throw httpError;
          }
          if (stdioError) {
            throw stdioError;
          }
          return found;
        })(),
        new Promise<never>((_, reject) => {
          timer = setTimeout(
            () =>
              reject(
                new Error(
                  "mcp attach failed server=mcp phase=attach error=mcp attach timeout",
                ),
              ),
            ATTACH_TIMEOUT_MS,
          );
        }),
      ]);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(message);
      throw new Error(message);
    } finally {
      if (timer !== undefined) {
        clearTimeout(timer);
      }
    }
    if (process.env.APIPI_MCP_CHECK !== "1") {
      return;
    }
    const found = playwright[0];
    if (!found) {
      const message =
        "mcp attach failed server=playwright phase=tools/list error=no playwright tools";
      console.error(message);
      throw new Error(message);
    }
    try {
      await runPlaywrightCheck(found.client, found.server, found.tools);
    } catch (err) {
      const message = err instanceof Error ? err.message : String(err);
      console.error(message);
      throw new Error(message);
    }
  });
}
