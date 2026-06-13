import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [react()],
  build: {
    rollupOptions: {
      output: {
        manualChunks: {
          react: ["react", "react-dom", "react-router-dom"],
          auth: ["supertokens-auth-react"],
          icons: ["lucide-react"],
        },
      },
    },
  },
  server: {
    port: 5173,
    proxy: {
      "/auth": "http://localhost:8000",
      "/chat": "http://localhost:8000",
      "/workspace": "http://localhost:8000",
      "/v1": "http://localhost:8000",
      "/health": "http://localhost:8000",
    },
  },
});
