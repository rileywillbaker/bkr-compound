import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// In dev, API calls proxy to the FastAPI server (VITE_API_PROXY inside
// docker compose; localhost when running vite directly).
const apiTarget = process.env.VITE_API_PROXY ?? "http://localhost:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: apiTarget, changeOrigin: true },
      "/ws": { target: apiTarget, ws: true },
      "/health": { target: apiTarget, changeOrigin: true },
    },
  },
});
