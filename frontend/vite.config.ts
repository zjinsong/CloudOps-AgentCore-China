import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时把 /api 代理到本地控制服务，避免 CORS。
export default defineConfig({
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
