import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { McpClient } from "./mcp_client.mjs";
import { HttpMcpClient } from "./mcp_http.mjs";

const DEFAULT_BASH_TIMEOUT_SEC = 120;
const ATTACH_TIMEOUT_MS = 15_000;

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

function agentFile(name: string): string {
  const dir = process.env.PI_CODING_AGENT_DIR;
  if (!dir) {
    return "";
  }
  try {
    return readFileSync(`${dir}/${name}`, "utf8").trim();
  } catch {
    console.error(`apipi prompt file missing: ${name}`);
    return "";
  }
}

function fill(template: string, values: Record<string, string>): string {
  return template.replace(/\$\{([A-Za-z_][A-Za-z0-9_]*)\}/g, (match, name: string) => {
    if (Object.prototype.hasOwnProperty.call(values, name)) {
      return values[name];
    }
    return match;
  });
}

function registerTools(
  pi: ExtensionAPI,
  label: string,
  tools: ListedTool[],
  client: RpcClient,
): string[] {
  const names: string[] = [];
  for (const tool of tools) {
    const name = `mcp_${sanitize(label)}_${sanitize(tool.name)}`;
    names.push(name);
    const lines = [
      fill(agentFile("mcp-tool.txt"), {
        tool_name: name,
        server_label: label,
        tool: tool.name,
      }),
    ].filter((line) => line);
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
        const started = Date.now();
        try {
          const result = await client.request("tools/call", {
            name: tool.name,
            arguments: params,
          });
          return contentOf(result);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          console.error(
            `mcp: tools/call ${name} failed after ${Date.now() - started}ms: ${message}`,
          );
          throw err;
        }
      },
    });
  }
  return names;
}

function workspaceRoot(): string {
  return process.env.HOME || "/workspace";
}

function writeImageCheck(tools: string[]): void {
  const dir = join(workspaceRoot(), "outputs");
  mkdirSync(dir, { recursive: true });
  let cmd = "";
  try {
    cmd = readFileSync(join(workspaceRoot(), ".apipi/pi-cmd"), "utf8");
  } catch {
    cmd = "";
  }
  const skill = join(workspaceRoot(), ".apipi/skills/browser/SKILL.md");
  let skillPresent = false;
  try {
    readFileSync(skill);
    skillPresent = true;
  } catch {
    skillPresent = false;
  }
  const report = {
    tools,
    skill: skillPresent && cmd.includes(".apipi/skills/browser"),
    playwright: tools.some((name) => name.startsWith("mcp_playwright_")),
  };
  writeFileSync(join(dir, "image-check.json"), `${JSON.stringify(report)}\n`);
}

async function attachStdio(pi: ExtensionAPI): Promise<string[]> {
  const deadline = Date.now() + ATTACH_TIMEOUT_MS;
  const remaining = (): number => Math.max(1, deadline - Date.now());
  const names: string[] = [];
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
    names.push(...registerTools(pi, server.label, listed.tools ?? [], client));
  }
  return names;
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
    registerTools(pi, server.label, listed.tools ?? [], client);
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
    let current = event.systemPrompt ?? "";
    try {
      const identity = readFileSync(`${dir}/identity.txt`, "utf8").trim();
      if (identity) {
        const replaced = replaceIntro(current, identity);
        if (replaced === null) {
          console.error("apipi identity intro was not found; keeping Pi's prompt");
        } else {
          current = replaced;
        }
      }
    } catch {
      void 0;
    }
    let raw = "";
    try {
      raw = readFileSync(`${dir}/capability.json`, "utf8");
    } catch {
      return current === event.systemPrompt ? undefined : { systemPrompt: current };
    }
    let parsed: { block?: string; overrides?: string[] } = {};
    try {
      parsed = JSON.parse(raw) as { block?: string; overrides?: string[] };
    } catch {
      return current === event.systemPrompt ? undefined : { systemPrompt: current };
    }
    const overrides = (parsed.overrides ?? []).filter((item) => item.trim());
    for (const line of overrides) {
      const suffix = `\n\n${line}`;
      if (current.endsWith(suffix)) {
        current = current.slice(0, -suffix.length);
      }
    }
    const date = new Date().toISOString().slice(0, 10);
    const block = (parsed.block ?? "").replace(/\$\{date\}/g, date).trim();
    const extra = [block, ...overrides].filter((item) => item).join("\n\n");
    if (!extra) {
      return current === event.systemPrompt ? undefined : { systemPrompt: current };
    }
    return { systemPrompt: `${current}\n\n${extra}` };
  });

  pi.on("tool_call", (event) => {
    if (event.toolName !== "bash") {
      return;
    }
    const input = event.input as { command?: string; timeout?: number };
    if (input.timeout === undefined) {
      input.timeout = DEFAULT_BASH_TIMEOUT_SEC;
    }
    return;
  });

  pi.on("session_start", async () => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let names: string[] = [];
    try {
      names = await Promise.race([
        (async () => {
          let httpError: Error | undefined;
          try {
            await attachHttp(pi);
          } catch (err) {
            httpError = err instanceof Error ? err : new Error(String(err));
          }
          let found: string[] = [];
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
    if (process.env.APIPI_IMAGE_CHECK === "1") {
      writeImageCheck(names);
    }
  });
}
