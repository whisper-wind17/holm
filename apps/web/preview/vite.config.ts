import { fileURLToPath, URL } from "node:url";

import { defineConfig } from "vite";

const previewRoot = fileURLToPath(new URL(".", import.meta.url));
const webRoot = fileURLToPath(new URL("..", import.meta.url));
const webPort = Number(process.env.HOLM_WEB_PORT ?? "5173");
const controlPort = Number(process.env.HOLM_CONTROL_PORT ?? "8000");
const gatewayPort = Number(process.env.HOLM_GATEWAY_PORT ?? "8001");

export default defineConfig({
  root: previewRoot,
  server: {
    fs: { allow: [webRoot] },
    host: "127.0.0.1",
    port: webPort,
    proxy: {
      "/api": { target: `http://127.0.0.1:${controlPort}` },
      "/v1": { target: `http://127.0.0.1:${gatewayPort}` },
    },
    strictPort: true,
  },
});
