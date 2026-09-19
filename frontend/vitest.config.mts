import path from "node:path";
import { fileURLToPath } from "node:url";
import { defineConfig } from "vitest/config";

const root = path.dirname(fileURLToPath(import.meta.url));

export default defineConfig({
  oxc: {
    jsx: "react-jsx",
  },
  resolve: {
    alias: { "@": root },
  },
  test: {
    environment: "jsdom",
    include: ["tests/**/*.test.tsx"],
    restoreMocks: true,
  },
});
