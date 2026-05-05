import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `base: "/ask/"` so the static bundle works under the Caddy `handle /ask/*`
// path block — every emitted asset URL is prefixed with /ask/.
export default defineConfig({
  base: "/ask/",
  plugins: [react()],
  build: {
    target: "es2020",
    sourcemap: false,
  },
  server: {
    port: 5174,
  },
});
