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
    },
  },
});
