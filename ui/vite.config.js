import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const proxyTarget = env.TIDAL_API_PROXY_TARGET || env.FACTORY_DASHBOARD_API_PROXY_TARGET || "http://localhost:8787";

  return {
    // Only public URL configuration belongs in browser code, including in dev.
    envPrefix: [],
    define: {
      "import.meta.env.VITE_TIDAL_API_BASE_URL": JSON.stringify(env.VITE_TIDAL_API_BASE_URL || ""),
      "import.meta.env.VITE_FACTORY_DASHBOARD_API_BASE_URL": JSON.stringify(env.VITE_FACTORY_DASHBOARD_API_BASE_URL || ""),
    },
    plugins: [react()],
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: proxyTarget,
          changeOrigin: true,
        },
      },
    },
    preview: {
      port: 4173,
    },
  };
});
