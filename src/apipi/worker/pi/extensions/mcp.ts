import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";

function httpServers(): Array<{ label: string; url: string; allowed: string[] }> {
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
    const allowedRaw = process.env[`APIPI_MCP_${index}_ALLOWED`];
    const allowed = allowedRaw ? allowedRaw.split(",").filter((item) => item) : [];
    servers.push({ label: (named || raw).trim() || "mcp", url, allowed });
  }
  return servers;
}

function workspaceRoot(): string {
  return process.env.HOME || "/workspace";
}

function writeImageCheck(): void {
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
    tools: [] as string[],
    skill: skillPresent && cmd.includes(".apipi/skills/browser"),
  };
  writeFileSync(join(dir, "image-check.json"), `${JSON.stringify(report)}\n`);
}

export default function (pi: ExtensionAPI) {
  pi.on("session_start", async () => {
    for (const server of httpServers()) {
      const toolExposure: Record<string, "direct"> = {};
      for (const name of server.allowed) {
        toolExposure[name] = "direct";
      }
      pi.registerMcpServer(server.label, {
        url: server.url,
        headers: { Authorization: "Bearer apipi" },
        exposure: server.allowed.length > 0 ? "hidden" : "direct",
        toolExposure,
        timeout: 15000,
      });
    }
    if (process.env.APIPI_IMAGE_CHECK === "1") {
      writeImageCheck();
    }
  });
}
