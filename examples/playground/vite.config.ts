import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");

  function gatewayOrigin(): string {
    const raw = env.API_BASE_URL || "http://localhost:8000/v1";
    return raw.replace(/\/v1\/?$/, "") || "http://localhost:8000";
  }

  function gatewayKey(): string {
    return env.API_KEY || "dev-token";
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

  return {
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
  };
});
