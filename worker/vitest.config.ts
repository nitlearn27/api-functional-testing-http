import { defineConfig } from "vitest/config";

// Pure-logic unit tests run in the default (node) environment — fast and simple. The
// parsing/matching/generation code is platform-agnostic; Durable Object / runtime behaviour
// is validated separately via `wrangler dev` smoke tests.
export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
  },
});
