const url = process.argv[2];

async function request(method, params) {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      Accept: "application/json, text/event-stream",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ jsonrpc: "2.0", id: 1, method, params }),
  });
  if (!response.ok) {
    throw new Error(`http ${response.status}`);
  }
  return response.json();
}

const init = await request("initialize", {
  protocolVersion: "2024-11-05",
  capabilities: {},
  clientInfo: { name: "test", version: "0" },
});
if (!init || init.result?.serverInfo?.name !== "fixture") {
  console.error("initialize failed", init);
  process.exit(1);
}
const listed = await request("tools/list", {});
const names = (listed.result?.tools || []).map((tool) => tool.name);
if (!names.includes("echo")) {
  console.error("tools/list failed", listed);
  process.exit(1);
}
const called = await request("tools/call", {
  name: "echo",
  arguments: { text: "hi" },
});
const text = JSON.stringify(called);
if (!text.includes("hi")) {
  console.error("tools/call failed", called);
  process.exit(1);
}
console.log("ok");
