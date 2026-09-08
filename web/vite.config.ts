import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: { outDir: "dist", emptyOutDir: true },
  server: {
    proxy: {
      "/api": {
        target: process.env.QT_WORKBENCH_API_ORIGIN ?? "http://127.0.0.1:8877",
        changeOrigin: true,
      },
    },
  },
});
