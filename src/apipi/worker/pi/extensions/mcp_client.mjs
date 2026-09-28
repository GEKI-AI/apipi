import { spawn } from "node:child_process";

const MCP_TIMEOUT_MS = 120_000;

export function encode(msg) {
  return Buffer.from(`${JSON.stringify(msg)}\n`);
}

export class McpClient {
  constructor(proc) {
    this.proc = proc;
    this.buf = Buffer.alloc(0);
    this.nextId = 1;
    this.pending = new Map();
    proc.stdout.on("data", (chunk) => {
      this.buf = Buffer.concat([this.buf, chunk]);
      this.drain();
    });
    proc.stderr.on("data", (chunk) => {
      const text = chunk.toString("utf8").trim();
      if (text) {
        console.error(`mcp: ${text.slice(0, 500)}`);
      }
    });
    proc.on("exit", () => {
      for (const waiter of this.pending.values()) {
        waiter.reject(new Error("mcp process exited"));
      }
      this.pending.clear();
    });
  }

  drain() {
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

  readOne() {
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
        return JSON.parse(json);
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
    return JSON.parse(line);
  }

  request(method, params, timeoutMs = MCP_TIMEOUT_MS) {
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

  notify(method, params) {
    this.proc.stdin.write(encode({ jsonrpc: "2.0", method, params }));
  }
}

export function spawnStdio(server) {
  return spawn(server.command, server.args, {
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
  });
}
