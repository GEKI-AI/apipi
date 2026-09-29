import { HttpMcpClient } from "../../src/apipi/worker/pi/extensions/mcp_http.mjs";

const url = process.argv[2];
const client = new HttpMcpClient(url);
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
await client.notify("notifications/initialized");
const listed = await client.request("tools/list", {}, 5000);
const names = (listed.tools || []).map((tool) => tool.name);
if (!names.includes("echo")) {
  console.error("tools/list failed", listed);
  process.exit(1);
}
const called = await client.request(
  "tools/call",
  { name: "echo", arguments: { text: "hi" } },
  5000,
);
const text = JSON.stringify(called);
if (!text.includes("hi")) {
  console.error("tools/call failed", called);
  process.exit(1);
}
console.log("ok");
