// Client-side share-link codec for the /ask conversation surface.
//
// Goal: produce a URL like `https://docs.aztec.network/ask#share=<blob>` that,
// when opened, replays the original Q&A in the recipient's browser without
// any server-side storage. We rely on the URL *hash* (not query string) so
// the encoded conversation never reaches the backend or Cloudflare Access
// logs — matches the existing anonymous-no-persist posture of /ask
// (`save_conversation: false`).
//
// Wire format (versioned, schema may evolve):
//   { v: 1, m: [{ r: "u" | "b", t: string, s?: ShareSource[] }] }
// — keys are deliberately short to keep the URL compact. Streaming/error
// state isn't persisted; everything reconstructed is "settled".
//
// Encoding: JSON → UTF-8 bytes → gzip (CompressionStream) → base64url.
// CompressionStream is the browser-native API; available on every
// evergreen browser since Safari 16.4 (Mar 2023). On older browsers we
// fall back to uncompressed base64url, which still works for short
// conversations.

import type { StreamSource } from "./stream";

export type ShareRole = "user" | "bot";

export type ShareMessage = {
  role: ShareRole;
  text: string;
  sources?: StreamSource[];
};

type WireMessage = {
  r: "u" | "b";
  t: string;
  s?: StreamSource[];
};

type WirePayload = {
  v: 1;
  m: WireMessage[];
};

// Hard cap so a runaway transcript can't generate a 200KB URL. Most
// link surfaces (Slack, address bar, email clients) handle ~8KB
// reliably; we leave headroom.
const MAX_ENCODED_BYTES = 32 * 1024;
// Cap on the inflated payload — sources in a shared payload are
// recipient-rendered, so a crafted hash must not be able to gzip-bomb
// the decoder into an arbitrary allocation.
const MAX_DECODED_BYTES = 256 * 1024;

const SHARE_HASH_PREFIX = "#share=";

// Reject `javascript:`, `data:`, `vbscript:`, `file:`, `blob:` and
// protocol-relative `//host` URLs. Allow `http(s):` absolute URLs and
// any same-origin relative path / fragment. `StreamSource.source`
// values from a shared payload are attacker-controlled (they came out
// of the URL hash), and `SourcesList` renders them directly as anchor
// hrefs — so this guards the click-to-XSS path on replay.
//
// Backslashes are rejected outright because the WHATWG URL parser
// normalizes `\` to `/` for special schemes, so `\\host/x` or
// `https:/\host/x` would otherwise round-trip into an external origin.
function isSafeHref(href: unknown): href is string {
  if (typeof href !== "string") return false;
  const trimmed = href.trim();
  if (!trimmed) return false;
  if (trimmed.includes("\\")) return false;
  if (trimmed.startsWith("//")) return false;
  if (/^[a-z][a-z0-9+.-]*:/i.test(trimmed)) {
    return /^https?:/i.test(trimmed);
  }
  return true;
}

function sanitizeSource(s: unknown): StreamSource | null {
  if (!s || typeof s !== "object") return null;
  const src = s as Record<string, unknown>;
  const out: StreamSource = {};
  if (isSafeHref(src.source)) out.source = src.source as string;
  if (typeof src.title === "string") out.title = src.title;
  if (typeof src.text === "string") out.text = src.text;
  // Drop entries that contributed no usable field — keeps the SourcesList
  // from rendering empty chips for crafted payloads that only carried
  // unsafe hrefs.
  if (!out.source && !out.title && !out.text) return null;
  return out;
}

function toWire(messages: ShareMessage[]): WirePayload {
  return {
    v: 1,
    m: messages.map((m) => {
      const wm: WireMessage = { r: m.role === "user" ? "u" : "b", t: m.text };
      if (m.sources && m.sources.length > 0) wm.s = m.sources;
      return wm;
    }),
  };
}

function fromWire(payload: unknown): ShareMessage[] | null {
  if (!payload || typeof payload !== "object") return null;
  const p = payload as { v?: unknown; m?: unknown };
  if (p.v !== 1 || !Array.isArray(p.m)) return null;
  const out: ShareMessage[] = [];
  for (const raw of p.m) {
    if (!raw || typeof raw !== "object") return null;
    const r = (raw as { r?: unknown }).r;
    const t = (raw as { t?: unknown }).t;
    const s = (raw as { s?: unknown }).s;
    if ((r !== "u" && r !== "b") || typeof t !== "string") return null;
    const msg: ShareMessage = { role: r === "u" ? "user" : "bot", text: t };
    if (Array.isArray(s)) {
      const cleaned = s
        .map(sanitizeSource)
        .filter((x): x is StreamSource => x !== null);
      if (cleaned.length > 0) msg.sources = cleaned;
    }
    out.push(msg);
  }
  return out;
}

function bytesToBase64Url(bytes: Uint8Array): string {
  // Chunk to keep the call stack happy on large buffers — String.fromCharCode
  // applied to a 50KB array via spread can blow the arg limit in some engines.
  let bin = "";
  const CHUNK = 0x8000;
  for (let i = 0; i < bytes.length; i += CHUNK) {
    bin += String.fromCharCode(...bytes.subarray(i, i + CHUNK));
  }
  return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

function base64UrlToBytes(s: string): Uint8Array {
  const padded = s.replace(/-/g, "+").replace(/_/g, "/") + "=".repeat((4 - (s.length % 4)) % 4);
  const bin = atob(padded);
  const out = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) out[i] = bin.charCodeAt(i);
  return out;
}

async function gzipBytes(bytes: Uint8Array): Promise<Uint8Array> {
  if (typeof CompressionStream === "undefined") {
    // Marker byte ("u" = uncompressed). Decoder distinguishes against
    // the gzip magic (0x1f) so this is unambiguous.
    const out = new Uint8Array(bytes.length + 1);
    out[0] = 0x75;
    out.set(bytes, 1);
    return out;
  }
  const stream = new Blob([bytes as BlobPart]).stream().pipeThrough(new CompressionStream("gzip"));
  const buf = await new Response(stream).arrayBuffer();
  return new Uint8Array(buf);
}

// Inflate gzipped bytes while capping the running output size so a
// crafted hash can't trigger an arbitrary allocation (gzip bomb).
async function gunzipBytes(bytes: Uint8Array, maxBytes: number): Promise<Uint8Array> {
  if (bytes.length === 0) return bytes;
  // Gzip magic is 0x1f 0x8b — anything starting with our "u" marker
  // came from the no-CompressionStream fallback path. Still cap it.
  if (bytes[0] === 0x75) {
    const payload = bytes.subarray(1);
    if (payload.byteLength > maxBytes) {
      throw new Error("Decompressed payload exceeds maximum size");
    }
    return payload;
  }
  if (typeof DecompressionStream === "undefined") {
    throw new Error("DecompressionStream not supported in this browser");
  }
  const stream = new Blob([bytes as BlobPart])
    .stream()
    .pipeThrough(new DecompressionStream("gzip"));
  const reader = stream.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > maxBytes) {
      await reader.cancel();
      throw new Error("Decompressed payload exceeds maximum size");
    }
    chunks.push(value);
  }
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return out;
}

export async function encodeShare(messages: ShareMessage[]): Promise<string> {
  const json = JSON.stringify(toWire(messages));
  const utf8 = new TextEncoder().encode(json);
  const compressed = await gzipBytes(utf8);
  const encoded = bytesToBase64Url(compressed);
  if (encoded.length > MAX_ENCODED_BYTES) {
    throw new Error("Conversation is too long to share as a link.");
  }
  return encoded;
}

export async function decodeShare(token: string): Promise<ShareMessage[] | null> {
  try {
    // Short-circuit before atob — a multi-MB hash should never reach
    // the base64 decoder or the decompressor.
    if (typeof token !== "string" || token.length > MAX_ENCODED_BYTES) {
      return null;
    }
    const bytes = base64UrlToBytes(token);
    const inflated = await gunzipBytes(bytes, MAX_DECODED_BYTES);
    const json = new TextDecoder("utf-8").decode(inflated);
    return fromWire(JSON.parse(json));
  } catch {
    return null;
  }
}

// Read a share token from `location.hash`. Returns the raw token (no
// `#share=` prefix) or null. Safe to call during SSR — guards on
// `typeof window`.
export function readShareHash(): string | null {
  if (typeof window === "undefined") return null;
  const hash = window.location.hash || "";
  if (!hash.startsWith(SHARE_HASH_PREFIX)) return null;
  const token = hash.slice(SHARE_HASH_PREFIX.length);
  return token.length > 0 ? token : null;
}

// Build a full shareable URL for the current origin/path with the
// given encoded token appended as `#share=...`. The query string is
// stripped — only the path + hash matter for replay.
export function buildShareUrl(token: string): string {
  if (typeof window === "undefined") return `#share=${token}`;
  const { origin, pathname } = window.location;
  return `${origin}${pathname}${SHARE_HASH_PREFIX}${token}`;
}

// Remove `#share=...` from the address bar without triggering navigation
// or a history entry. Used after the recipient has consumed the shared
// transcript, so subsequent local changes (new questions) don't get
// confused with the shared replay.
export function clearShareHash(): void {
  if (typeof window === "undefined") return;
  const { origin, pathname, search } = window.location;
  window.history.replaceState(null, "", `${origin}${pathname}${search}`);
}

// Async Clipboard API where available (secure contexts: HTTPS +
// localhost) with a hidden-textarea + `document.execCommand("copy")`
// fallback so the button still works in older WebViews or sandboxed
// iframes. Returns whether the copy succeeded.
export async function copyToClipboard(text: string): Promise<boolean> {
  if (typeof navigator !== "undefined" && navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      /* fall through to execCommand */
    }
  }
  if (typeof document === "undefined") return false;
  const ta = document.createElement("textarea");
  try {
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    if (ta.parentNode) ta.parentNode.removeChild(ta);
  }
}
