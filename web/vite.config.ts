import { defineConfig, loadEnv } from "vite";

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "CONSOLE_");
  const backend = env.CONSOLE_BACKEND_URL;
  if (backend) {
    const url = new URL(backend);
    if (!["http:", "https:"].includes(url.protocol) || url.username || url.password
        || url.search || url.hash || url.pathname !== "/") throw new Error("CONSOLE_BACKEND_URL must be an origin without credentials");
  }
  return {
    server: { host: "127.0.0.1", port: 5178, strictPort: true,
      ...(backend ? { proxy: { "/api": { target: backend } } } : {}) },
    preview: { host: "127.0.0.1", port: 4178, strictPort: true },
    build: { outDir: "dist" },
  };
});
