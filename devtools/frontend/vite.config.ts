import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig({
  plugins: [react()],
  build: {
    outDir: "../../src/kivyfn/store/devtools/static",
    emptyOutDir: true,
    sourcemap: false,
  },
  test: { include: ["src/**/*.test.ts"] },
});
