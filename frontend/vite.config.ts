import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In Docker the backend is a separate service ("backend"); locally it's localhost.
const backendTarget = process.env.VITE_BACKEND_URL ?? "http://localhost:8000";

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
      // SuperTokens API only — must NOT match the website UI path (/auth-ui),
      // which React Router serves. Negative lookahead excludes /auth-ui*.
      "^/auth(?!-ui)": backendTarget,
      "/chat": backendTarget,
      "/workspace": backendTarget,
      "/v1": backendTarget,
      "/health": backendTarget,
    },
  },
});
