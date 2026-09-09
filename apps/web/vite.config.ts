import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, ".", "ENDLESS_TASK_");
  const apiTarget = env.ENDLESS_TASK_API_URL || "http://127.0.0.1:8000";

  return {
    plugins: [react()],
    server: {
      host: "127.0.0.1",
      proxy: {
        "/api": apiTarget,
        "/capabilities": apiTarget,
        "/conversations": apiTarget,
        "/filesystem": apiTarget,
        "/health": apiTarget,
        "/turns": apiTarget,
        "/approvals": apiTarget,
        "/artifacts": apiTarget,
        "/artifact-proposals": apiTarget,
        "/mcp": apiTarget,
        "/task-proposals": apiTarget,
        "/tasks": apiTarget,
        "/notifications": apiTarget,
        "/memories": apiTarget,
        "/memory-proposals": apiTarget,
        "/providers": apiTarget,
        "/settings": apiTarget,
        "/skills": apiTarget,
        "/knowledge-sources": apiTarget,
        "/knowledge-proposals": apiTarget,
        "/proposals": apiTarget,
        "/reminders": apiTarget,
        "/retrieval-events": apiTarget,
        "/retrieval-stats": apiTarget,
        "/search": apiTarget,
        // S12：终端 WebSocket 走同一条路径，必须显式开启 ws 升级转发，
        // 否则浏览器连不上 /workspaces/{id}/terminals/{sid}。
        "/workspaces": { target: apiTarget, ws: true },
      },
    },
  };
});
