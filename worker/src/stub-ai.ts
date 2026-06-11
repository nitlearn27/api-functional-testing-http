/**
 * Stub for the optional `ai` (Vercel AI SDK) dependency.
 *
 * The `agents` SDK dynamically `import("ai")` only on its MCP *client* / getAITools path,
 * which this MCP *server* never exercises. Aliasing `ai` here (see wrangler.jsonc) avoids
 * bundling the full AI SDK. If a code path ever actually calls jsonSchema(), it throws loudly
 * rather than silently misbehaving.
 */
export function jsonSchema(): never {
  throw new Error("the 'ai' SDK is stubbed out in this MCP server build");
}
