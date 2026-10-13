/* util.js — shared helpers: formatting, CRC32, SHA-256 (Web Worker),
 * relay frame codec, HTTP JSON wrapper. No frameworks, ES2022+. */

/* ------------------------------------------------------------ format --- */

export function fmtSize(bytes) {
  if (bytes === 0) return "0 B";
  if (!Number.isFinite(bytes)) return "—";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(units.length - 1, Math.floor(Math.log(Math.abs(bytes)) / Math.log(1024)));
  const value = bytes / 1024 ** i;
  return `${value >= 100 || i === 0 ? Math.round(value) : value.toFixed(1)} ${units[i]}`;
}

export function fmtSpeed(bytesPerSecond) {
  if (!bytesPerSecond || bytesPerSecond <= 0) return "—";
  return `${fmtSize(bytesPerSecond)}/s`;
}

export function fmtTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "--:--:--";
  return d.toLocaleTimeString([], { hour12: false });
}

export function fmtPercent(value) {
  return `${Math.max(0, Math.min(100, value ?? 0)).toFixed(1)}%`;
}

export function relTime(iso) {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const seconds = Math.max(0, (Date.now() - d.getTime()) / 1000);
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  return `${Math.floor(seconds / 86400)}d ago`;
}

export function esc(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

export function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export function osIcon(os) {
  switch (os) {
    case "Android": return "🤖";
    case "iOS": return "📱";
    case "Windows": return "🪟";
    case "macOS": return "🖥️";
    case "Linux": return "🐧";
    default: return "📦";
  }
}

export function kindArrow(kind) {
  switch (kind) {
    case "upload": return "⬆";
    case "download": return "⬇";
    case "relay": return "➡";
    default: return "•";
  }
}

export function notify(message) {
  window.dispatchEvent(new CustomEvent("filehub:notice", { detail: message }));
}

/* --------------------------------------------------------------- http --- */

export async function apiPost(url, payload) {
  try {
    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload ?? {}),
    });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  } catch (error) {
    return { ok: false, status: 0, data: { error: String(error.message || error) } };
  }
}

export async function apiDelete(url) {
  try {
    const response = await fetch(url, { method: "DELETE" });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  } catch (error) {
    return { ok: false, status: 0, data: { error: String(error.message || error) } };
  }
}

/* --------------------------------------------------------------- crc32 --- */

const CRC_TABLE = (() => {
  const table = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
    table[n] = c >>> 0;
  }
  return table;
})();

export function crc32(bytes) {
  let crc = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) {
    crc = CRC_TABLE[(crc ^ bytes[i]) & 0xff] ^ (crc >>> 8);
  }
  return (crc ^ 0xffffffff) >>> 0;
}

/* ------------------------------------------------------ sha-256 worker --- */
/* crypto.subtle is unavailable on insecure LAN origins (http://192.168.x.x),
 * so SHA-256 runs in a pure-JS Web Worker — one implementation shared by
 * uploads (§11.1) and relay verification (§13). */

const WORKER_SOURCE = String.raw`
const K = new Uint32Array([
  0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,
  0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,
  0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,
  0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,
  0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,
  0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,
  0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,
  0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2
]);
function rotr(x, n) { return (x >>> n) | (x << (32 - n)); }
const states = new Map();
function create() {
  const H = new Uint32Array([0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19]);
  const w = new Uint32Array(64);
  const buf = new Uint8Array(64);
  let bufLen = 0;
  let total = 0;
  function block(b, off) {
    for (let i = 0; i < 16; i++) {
      w[i] = (b[off + i * 4] << 24) | (b[off + i * 4 + 1] << 16) | (b[off + i * 4 + 2] << 8) | b[off + i * 4 + 3];
    }
    for (let i = 16; i < 64; i++) {
      const s0 = rotr(w[i - 15], 7) ^ rotr(w[i - 15], 18) ^ (w[i - 15] >>> 3);
      const s1 = rotr(w[i - 2], 17) ^ rotr(w[i - 2], 19) ^ (w[i - 2] >>> 10);
      w[i] = (w[i - 16] + s0 + w[i - 7] + s1) >>> 0;
    }
    let a = H[0], b2 = H[1], c = H[2], d = H[3], e = H[4], f = H[5], g = H[6], h = H[7];
    for (let i = 0; i < 64; i++) {
      const S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
      const ch = (e & f) ^ (~e & g);
      const t1 = (h + S1 + ch + K[i] + w[i]) >>> 0;
      const S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
      const maj = (a & b2) ^ (a & c) ^ (b2 & c);
      const t2 = (S0 + maj) >>> 0;
      h = g; g = f; f = e; e = (d + t1) >>> 0; d = c; c = b2; b2 = a; a = (t1 + t2) >>> 0;
    }
    H[0] = (H[0] + a) >>> 0; H[1] = (H[1] + b2) >>> 0; H[2] = (H[2] + c) >>> 0; H[3] = (H[3] + d) >>> 0;
    H[4] = (H[4] + e) >>> 0; H[5] = (H[5] + f) >>> 0; H[6] = (H[6] + g) >>> 0; H[7] = (H[7] + h) >>> 0;
  }
  return {
    update(data) {
      total += data.length;
      let i = 0;
      if (bufLen > 0) {
        const take = Math.min(64 - bufLen, data.length);
        buf.set(data.subarray(0, take), bufLen);
        bufLen += take;
        i = take;
        if (bufLen === 64) { block(buf, 0); bufLen = 0; }
      }
      for (; i + 64 <= data.length; i += 64) block(data, i);
      if (i < data.length) { buf.set(data.subarray(i), 0); bufLen = data.length - i; }
    },
    digest() {
      const padLen = bufLen < 56 ? 64 : 128;
      const tail = new Uint8Array(padLen);
      tail.set(buf.subarray(0, bufLen), 0);
      tail[bufLen] = 0x80;
      const bitsLo = (total * 8) >>> 0;
      const bitsHi = Math.floor(total / 0x20000000);
      const view = new DataView(tail.buffer);
      view.setUint32(padLen - 8, bitsHi, false);
      view.setUint32(padLen - 4, bitsLo, false);
      block(tail, 0);
      if (padLen === 128) block(tail, 64);
      let hex = "";
      for (let i = 0; i < 8; i++) hex += H[i].toString(16).padStart(8, "0");
      return hex;
    }
  };
}
async function hashBlob(blob) {
  const sha = create();
  const BLOCK = 4 * 1024 * 1024;
  for (let offset = 0; offset < blob.size; offset += BLOCK) {
    const part = await blob.slice(offset, Math.min(offset + BLOCK, blob.size)).arrayBuffer();
    sha.update(new Uint8Array(part));
  }
  return sha.digest();
}
self.onmessage = async (event) => {
  const msg = event.data;
  try {
    if (msg.op === "hash-blob") {
      self.postMessage({ id: msg.id, hex: await hashBlob(msg.blob) });
    } else if (msg.op === "hash-start") {
      states.set(msg.id, create());
      self.postMessage({ id: msg.id, ok: true });
    } else if (msg.op === "hash-update") {
      const sha = states.get(msg.id);
      if (sha) sha.update(new Uint8Array(msg.buffer));
      self.postMessage({ id: msg.id, ok: true });
    } else if (msg.op === "hash-end") {
      const sha = states.get(msg.id);
      states.delete(msg.id);
      self.postMessage({ id: msg.id, hex: sha ? sha.digest() : null });
    }
  } catch (error) {
    self.postMessage({ id: msg.id, error: String((error && error.message) || error) });
  }
};
`;

let workerInstance = null;
let requestSeq = 0;
const pendingRequests = new Map();

function getWorker() {
  if (workerInstance) return workerInstance;
  const blob = new Blob([WORKER_SOURCE], { type: "text/javascript" });
  workerInstance = new Worker(URL.createObjectURL(blob));
  workerInstance.onmessage = (event) => {
    const entry = pendingRequests.get(event.data.id);
    if (!entry) return;
    pendingRequests.delete(event.data.id);
    if (event.data.error) entry.reject(new Error(event.data.error));
    else entry.resolve(event.data);
  };
  workerInstance.onerror = (event) => {
    const error = new Error(event.message || "hash worker failed");
    for (const entry of pendingRequests.values()) entry.reject(error);
    pendingRequests.clear();
  };
  return workerInstance;
}

function workerRequest(message, transfer) {
  return new Promise((resolve, reject) => {
    pendingRequests.set(message.id, { resolve, reject });
    getWorker().postMessage(message, transfer);
  });
}

/** SHA-256 of a File/Blob as lowercase hex (runs in a Web Worker). */
export async function sha256Blob(blob) {
  return (await workerRequest({ op: "hash-blob", id: ++requestSeq, blob })).hex;
}

/** Streaming SHA-256 across chunk arrivals: start → update* → finish. */
export function createHasher() {
  const id = ++requestSeq;
  return {
    async start() {
      await workerRequest({ op: "hash-start", id });
    },
    async update(bytes) {
      const buffer = bytes.buffer.slice(
        bytes.byteOffset,
        bytes.byteOffset + bytes.byteLength
      );
      await workerRequest({ op: "hash-update", id, buffer }, [buffer]);
    },
    async finish() {
      return (await workerRequest({ op: "hash-end", id })).hex;
    },
  };
}

/* -------------------------------------------------------- relay frames --- */
/* [4B id length][id utf-8][8B chunk index][4B crc32][payload]  (§7.3) */

const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();

export function buildRelayFrame(transferId, index, checksum, payload) {
  const idBytes = textEncoder.encode(transferId);
  const header = 4 + idBytes.length + 8 + 4;
  const frame = new Uint8Array(header + payload.length);
  const view = new DataView(frame.buffer);
  view.setUint32(0, idBytes.length, false);
  frame.set(idBytes, 4);
  view.setUint32(4 + idBytes.length, Math.floor(index / 0x100000000), false);
  view.setUint32(8 + idBytes.length, index >>> 0, false);
  view.setUint32(12 + idBytes.length, checksum >>> 0, false);
  frame.set(payload, header);
  return frame;
}

export function parseRelayFrame(data) {
  const bytes = data instanceof Uint8Array ? data : new Uint8Array(data);
  const view = new DataView(bytes.buffer, bytes.byteOffset, bytes.byteLength);
  if (bytes.length < 4) throw new Error("frame too short");
  const idLen = view.getUint32(0, false);
  if (idLen === 0 || idLen > 64) throw new Error("bad transfer id length");
  const header = 4 + idLen + 8 + 4;
  if (bytes.length < header) throw new Error("frame header truncated");
  const transferId = textDecoder.decode(bytes.subarray(4, 4 + idLen));
  const indexHi = view.getUint32(4 + idLen, false);
  const indexLo = view.getUint32(8 + idLen, false);
  const index = indexHi * 0x100000000 + indexLo;
  const checksum = view.getUint32(12 + idLen, false);
  return { transferId, index, checksum, payload: bytes.subarray(header) };
}

/* ------------------------------------------------------------- bitmap --- */

/** Hex bitmap (server format) → sorted chunk indices that are missing. */
export function missingFromBitmap(hex, totalChunks) {
  const missing = [];
  if (!hex) return Array.from({ length: totalChunks }, (_, i) => i);
  const bytes = new Uint8Array(
    hex.match(/.{1,2}/g)?.map((pair) => parseInt(pair, 16)) ?? []
  );
  for (let i = 0; i < totalChunks; i++) {
    const byte = bytes[i >> 3];
    const bit = byte === undefined ? 0 : (byte >> (7 - (i & 7))) & 1;
    if (!bit) missing.push(i);
  }
  return missing;
}
