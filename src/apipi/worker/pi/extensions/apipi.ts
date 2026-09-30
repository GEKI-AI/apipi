import { readFileSync } from "node:fs";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

const DEFAULT_BASH_TIMEOUT_SEC = 120;
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
}
