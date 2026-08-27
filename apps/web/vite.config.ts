import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    proxy: {
      "/conversations": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
      "/turns": "http://127.0.0.1:8000",
      "/approvals": "http://127.0.0.1:8000",
      "/artifacts": "http://127.0.0.1:8000",
      "/artifact-proposals": "http://127.0.0.1:8000",
      "/task-proposals": "http://127.0.0.1:8000",
      "/tasks": "http://127.0.0.1:8000",
      "/notifications": "http://127.0.0.1:8000",
      "/memories": "http://127.0.0.1:8000",
      "/memory-proposals": "http://127.0.0.1:8000",
      "/settings": "http://127.0.0.1:8000",
      "/knowledge-sources": "http://127.0.0.1:8000",
      "/knowledge-proposals": "http://127.0.0.1:8000",
      "/proposals": "http://127.0.0.1:8000",
      "/reminders": "http://127.0.0.1:8000",
      "/retrieval-events": "http://127.0.0.1:8000",
      "/retrieval-stats": "http://127.0.0.1:8000",
      "/search": "http://127.0.0.1:8000",
    },
  },
});
