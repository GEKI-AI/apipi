import { spawn } from "node:child_process";
import { McpClient } from "../../src/apipi/worker/pi/extensions/mcp_client.mjs";

const proc = spawn(process.argv[2], process.argv.slice(3), {
  stdio: ["pipe", "pipe", "pipe"],
});
const client = new McpClient(proc);
const init = await client.request(
  "initialize",
  {
    protocolVersion: "2024-11-05",
    capabilities: {},
    clientInfo: { name: "test", version: "0" },
  },
  5000,
);
if (!init || init.serverInfo?.name !== "fixture") {
  console.error("initialize failed", init);
  process.exit(1);
}
client.notify("notifications/initialized");
const listed = await client.request("tools/list", {}, 5000);
const names = (listed.tools || []).map((tool) => tool.name);
if (!names.includes("echo")) {
  console.error("tools/list failed", listed);
  process.exit(1);
}
proc.kill();
console.log("ok");
