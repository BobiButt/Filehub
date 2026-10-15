/* uploader.js — chunked parallel HTTP upload to the server (§11.1).
 *
 * Flow: /api/upload/init → N parallel lanes of 1 MB POSTs (backpressure:
 * each lane awaits its chunk) → /complete.
 * Files under 30 MB are SHA-256 hashed client-side; larger files skip the
 * client hash and rely on the server's post-assembly verification.
 * On any failure: pause, then resume from the server's bitmap.
 */

import { store, emit } from "./ws.js";
import { apiPost, crc32, sha256Blob, missingFromBitmap, sleep } from "./util.js";

/** Local upload rows keyed by a client-side id; Panel 1 renders these. */
export const uploads = new Map();

const HASH_THRESHOLD = 30 * 1024 * 1024; // 30 MB — larger files skip client hash
const FATAL_STATUSES = new Set([401, 403, 404, 409, 413]);

class FatalError extends Error {}

function update(entry, changes) {
  Object.assign(entry, changes);
  emit("uploads-changed");
}

function removeLater(entry, delay = 1500) {
  setTimeout(() => {
    uploads.delete(entry.id);
    emit("uploads-changed");
  }, delay);
}

/** Upload one file; resolves when done or permanently failed. */
export async function uploadFile(file) {
  const entry = {
    id: (crypto.randomUUID?.() ?? `u-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`),
    sessionId: null,
    filename: file.name,
    size: file.size,
    percent: 0,
    phase: "hashing",
    error: null,
  };
  uploads.set(entry.id, entry);
  emit("uploads-changed");
  console.log("[upload] start:", file.name, file.size, "bytes");

  // Only hash small files client-side. Large files start uploading instantly
  // and let the server verify the whole-file SHA-256 after assembly.
  let sha256 = "";
  if (file.size < HASH_THRESHOLD) {
    try {
      sha256 = await sha256Blob(file);
    } catch (error) {
      console.warn("[upload] hash failed:", error);
      update(entry, { phase: "failed", error: `hash failed: ${error.message}` });
      return;
    }
  }
  console.log("[upload] sha256:", sha256 || "(skipped)");

  const chunkSizeDefault = window.__FILEHUB__?.chunkSize || 1048576;
  let attempt = 0;
  let repairs = 0;

  while (true) {
    try {
      update(entry, {
        phase: attempt === 0 ? "uploading" : "resuming",
        error: null,
      });

      const init = await apiPost("/api/upload/init", {
        filename: file.name,
        size: file.size,
        sha256,
        chunk_size: chunkSizeDefault,
      });
      console.log("[upload] init →", init.status, init.data);

      if (!init.ok) {
        const message = init.data?.error || `init failed (${init.status})`;
        if (FATAL_STATUSES.has(init.status)) throw new FatalError(message);
        throw new Error(message);
      }

      const chunkSize = init.data.chunk_size || chunkSizeDefault;
      const total = file.size === 0 ? 0 : Math.ceil(file.size / chunkSize);
      entry.sessionId = init.data.session_id;

      let missing;
      if (init.data.existing_bitmap) {
        missing = missingFromBitmap(init.data.existing_bitmap, total);
        update(entry, { phase: missing.length ? "resuming" : "verifying" });
      } else {
        missing = Array.from({ length: total }, (_, i) => i);
      }
      console.log("[upload] chunks:", missing.length, "of", total, "to send");

      if (missing.length) {
        update(entry, { phase: "uploading" });
        await sendChunks(entry, file, missing, chunkSize);
      }

      update(entry, { phase: "verifying", percent: 100 });
      let result = await apiPost("/api/upload/complete", {
        session_id: entry.sessionId,
      });
      console.log("[upload] complete →", result.status, result.data);

      // Whole-file SHA mismatch → targeted chunk repair. Only meaningful
      // when the client provided a sha256; skip otherwise.
      if (result.status === 422 && sha256) {
        repairs += 1;
        if (repairs > 2) throw new FatalError("sha256 mismatch after repair");
        update(entry, { phase: "repairing" });
        const repaired = await repairChunks(entry, file, sha256, chunkSize);
        if (repaired) {
          update(entry, { phase: "verifying" });
          result = await apiPost("/api/upload/complete", {
            session_id: entry.sessionId,
          });
          console.log(
            "[upload] complete (after repair) →",
            result.status,
            result.data
          );
        }
      }

      if (!result.ok) {
        const message = result.data?.error || `complete failed (${result.status})`;
        if (FATAL_STATUSES.has(result.status)) throw new FatalError(message);
        throw new Error(message);
      }

      update(entry, { phase: "done", percent: 100 });
      console.log("[upload] done:", entry.filename);
      removeLater(entry);
      return;
    } catch (error) {
      if (error instanceof FatalError) {
        console.warn("[upload] fatal:", error.message);
        update(entry, { phase: "failed", error: error.message });
        removeLater(entry, 8000);
        return;
      }
      attempt += 1;
      if (attempt > 50) {
        console.warn("[upload] giving up after", attempt, "attempts:", error.message);
        update(entry, {
          phase: "failed",
          error: error.message || "network error",
        });
        removeLater(entry, 8000);
        return;
      }
      console.warn("[upload] paused (attempt", attempt + "):", error.message || "network error");
      update(entry, {
        phase: "paused",
        error: error.message || "network error",
      });
      await sleep(Math.min(2000 * attempt, 10000));
    }
  }
}

/** Upload files one after another so lanes are never oversubscribed. */
export async function uploadFiles(fileList) {
  for (const file of fileList) {
    // eslint-disable-next-line no-await-in-loop
    await uploadFile(file);
  }
}

/**
 * Send the given chunk indices over up to `yourLanes` parallel lanes.
 * Each lane awaits its POST before taking the next chunk (backpressure).
 */
async function sendChunks(entry, file, indices, chunkSize) {
  const lanes = Math.max(
    1,
    Math.min(store.scheduler.your_lanes || 4, indices.length)
  );
  const queue = [...indices];
  console.log("[upload] sendChunks:", indices.length, "chunk(s),", lanes, "lane(s)");

  const runLane = async () => {
    while (queue.length) {
      const index = queue.shift();
      const start = index * chunkSize;
      const end = Math.min(start + chunkSize, file.size);
      const buffer = await file.slice(start, end).arrayBuffer();
      const checksum = crc32(new Uint8Array(buffer));

      const form = new FormData();
      form.append("session_id", entry.sessionId);
      form.append("index", String(index));
      form.append("crc32", String(checksum));
      form.append("payload", new Blob([buffer]), `${file.name}.part`);

      const response = await fetch("/api/upload/chunk", {
        method: "POST",
        body: form,
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const error = new Error(
          data.error || `chunk ${index} failed (${response.status})`
        );
        error.status = response.status;
        throw error;
      }
      const percent =
        data.total > 0
          ? Math.round((1000 * data.received) / data.total) / 10
          : 100;
      update(entry, { percent });
    }
  };

  await Promise.all(Array.from({ length: lanes }, () => runLane()));
}

/**
 * SHA mismatch repair (§13): compute per-chunk CRC32s locally, hand them
 * to /api/upload/resume, and resend exactly the chunks that differ.
 * Returns true when at least one chunk was resent.
 */
async function repairChunks(entry, file, sha256, chunkSize) {
  const total = file.size === 0 ? 0 : Math.ceil(file.size / chunkSize);
  const crcs = [];
  for (let index = 0; index < total; index++) {
    const start = index * chunkSize;
    const end = Math.min(start + chunkSize, file.size);
    const buffer = await file.slice(start, end).arrayBuffer();
    crcs.push(crc32(new Uint8Array(buffer)));
    if (index % 32 === 31) await sleep(0); // keep the UI thread responsive
  }

  const resume = await apiPost("/api/upload/resume", {
    filename: file.name,
    size: file.size,
    sha256,
    crcs,
  });
  if (!resume.ok || !resume.data.missing?.length) return false;
  await sendChunks(entry, file, resume.data.missing, chunkSize);
  return true;
}