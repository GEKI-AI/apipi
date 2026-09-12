import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

function gatewayOrigin(): string {
  const raw = process.env.OPENAI_BASE_URL ?? "http://localhost:8000/v1";
  return raw.replace(/\/v1\/?$/, "") || "http://localhost:8000";
}

function gatewayKey(): string {
  return process.env.OPENAI_API_KEY ?? "dev-token";
}

const proxy = {
  "/v1": {
    target: gatewayOrigin(),
    changeOrigin: true,
    timeout: 0,
    configure(proxyServer) {
      const key = gatewayKey();
      proxyServer.on("proxyReq", (proxyReq) => {
        proxyReq.setHeader("Authorization", `Bearer ${key}`);
      });
    },
  },
};

export default defineConfig({
  plugins: [react()],
  server: {
    host: "0.0.0.0",
    port: 8100,
    proxy,
  },
  preview: {
    host: "0.0.0.0",
    port: 8100,
    proxy,
  },
});
