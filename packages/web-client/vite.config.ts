import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  base: process.env.BASE_PATH || "./",
  server: {
    port: 5173,
  },
  test: {
    environment: "jsdom",
    globals: true,
  },
});
