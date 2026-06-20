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
      // Backend API is mounted at the SINGULAR "/workspace/..." prefix, but the
      // SPA's routes live under the PLURAL "/workspaces" (e.g. /workspaces,
      // /workspaces/:id). A bare "/workspace" prefix rule matches by prefix and
      // so greedily swallows "/workspaces*" too, proxying the UI route to the
      // backend — which 404s with {"detail":"Not Found"} on a pasted URL or
      // refresh. Anchor to "/workspace/" (and a bare "/workspace") so only the
      // API is proxied; "/workspaces" never starts with "/workspace/".
      "^/workspace(/|$)": backendTarget,
      "/v1": backendTarget,
      "/health": backendTarget,
    },
  },
});
