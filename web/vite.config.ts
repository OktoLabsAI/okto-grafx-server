import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { fileURLToPath, URL } from "node:url";

const daemon = process.env.OKG_DAEMON ?? "http://127.0.0.1:8737";

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: { "@": fileURLToPath(new URL("./src", import.meta.url)) },
  },
  build: {
    // The daemon serves this directory: one artifact, one process.
    outDir: "../src/okto_grafx_server/web",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    port: 5173,
    proxy: {
      "/api": { target: daemon, changeOrigin: false },
      "/admin/api": { target: daemon, changeOrigin: false },
      "/mcp": { target: daemon, changeOrigin: false },
      "/metrics": { target: daemon, changeOrigin: false },
    },
  },
  test: {
    environment: "jsdom",
    globals: true,
    setupFiles: "./src/test/setup.ts",
    css: false,
  },
});
