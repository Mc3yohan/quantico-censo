import { defineConfig } from "vite";
import { viteSingleFile } from "vite-plugin-singlefile";

export default defineConfig(({ mode }) => {
  const isPages = mode === "pages";
  return {
    base: "./",
    plugins: isPages ? [viteSingleFile()] : [],
    build: {
      outDir: isPages ? "../docs" : "dist",
      emptyOutDir: true,
      target: "es2022",
      assetsInlineLimit: isPages ? 100_000_000 : 4096,
    },
    server: {
      port: 5173,
      proxy: {
        "/api": {
          target: "http://127.0.0.1:8095",
          changeOrigin: true,
          ws: true,
        },
      },
    },
  };
});
