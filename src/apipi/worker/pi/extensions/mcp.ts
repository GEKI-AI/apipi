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
  });
}
