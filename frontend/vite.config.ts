import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { resolve } from 'node:path';

// 开发时把 /api 代理到本地 AG-UI 服务(默认 8099),避免 CORS
export default defineConfig({
  resolve: { alias: {
    'dcv-ui': resolve('node_modules/bedrock-agentcore/dist/src/tools/browser/live-view/nice-dcv-web-client-sdk/dcv-ui/dcv-ui.js'),
    'dcv': resolve('node_modules/bedrock-agentcore/dist/src/tools/browser/live-view/nice-dcv-web-client-sdk/dcvjs-esm/dcv.js'),
  } },
  plugins: [react()],
  server: {
    port: 3000,
    proxy: {
      "/api": {
        target: process.env.CLOUDOPS_URL || "http://127.0.0.1:8098",
        changeOrigin: false,
        ws: true,
      },
    },
  },
});
