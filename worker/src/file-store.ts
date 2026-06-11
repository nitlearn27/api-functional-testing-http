/**
 * FileStore — a tiny Durable Object that holds one generated file (xlsx bytes) and serves it.
 *
 * Used so the MCP tools can return a short *download URL* instead of a large base64 blob: a
 * base64 workbook forces the client-side model to ingest (and, when saving, regenerate) tens of
 * thousands of characters, which is slow and unreliable. Here the bytes live in a DO addressed
 * by a random id, served at `GET /files/{id}`. Each file self-deletes after a TTL via an alarm.
 *
 * This is the no-R2 path (R2 needs a dashboard enable); Durable Objects are already in use.
 */
import { DurableObject } from "cloudflare:workers";
import type { Env } from "./env.js";

const EXPIRY_MS = 2 * 60 * 60 * 1000; // files auto-delete after 2 hours
export const XLSX_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";

interface FileMeta {
  filename: string;
  contentType: string;
}

export class FileStore extends DurableObject<Env> {
  async store(bytes: Uint8Array, filename: string, contentType: string): Promise<void> {
    await this.ctx.storage.put("blob", bytes);
    await this.ctx.storage.put("meta", { filename, contentType } satisfies FileMeta);
    await this.ctx.storage.setAlarm(Date.now() + EXPIRY_MS);
  }

  async alarm(): Promise<void> {
    await this.ctx.storage.deleteAll();
  }

  async fetch(): Promise<Response> {
    const blob = await this.ctx.storage.get<Uint8Array>("blob");
    const meta = await this.ctx.storage.get<FileMeta>("meta");
    if (!blob || !meta) return new Response("Not found", { status: 404 });
    return new Response(blob, {
      headers: {
        "Content-Type": meta.contentType,
        "Content-Disposition": `attachment; filename="${meta.filename}"`,
        "Cache-Control": "no-store",
      },
    });
  }

  /** Return the stored bytes (or null if absent/expired) — used to run a server-stored suite. */
  async getBytes(): Promise<Uint8Array | null> {
    return (await this.ctx.storage.get<Uint8Array>("blob")) ?? null;
  }
}

/** Store bytes in a fresh FileStore DO; returns its id and a public download URL. */
export async function storeFile(
  env: Env,
  bytes: Uint8Array,
  filename: string,
  contentType: string = XLSX_CONTENT_TYPE,
): Promise<{ id: string; url: string }> {
  const id = crypto.randomUUID();
  await env.FILES.get(env.FILES.idFromName(id)).store(bytes, filename, contentType);
  const base = (env.PUBLIC_BASE_URL ?? "").replace(/\/+$/, "");
  return { id, url: `${base}/files/${id}` };
}

/** Load bytes previously stored under `id` (null if missing/expired). */
export async function loadFileBytes(env: Env, id: string): Promise<Uint8Array | null> {
  return env.FILES.get(env.FILES.idFromName(id)).getBytes();
}
