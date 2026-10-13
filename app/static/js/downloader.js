/* downloader.js — server → browser downloads (§11.2).

The file is NEVER buffered whole in JS memory:
 - default: a native <a download> — the browser streams straight to disk
   with its own progress UI and native Range/resume support;
 - large files on browsers with the File System Access API: fetch +
   ReadableStream written chunk-by-chunk to disk (TCP backpressure via
   awaiting writes) with live progress in Panel 3. If the save picker is
   unavailable or refuses, we fall back to the native download. */

import { emit } from "./ws.js";
import { notify } from "./util.js";

const PROGRESS_THRESHOLD = 32 * 1024 * 1024; // 32 MB
const fsAccessAvailable = typeof window.showSaveFilePicker === "function";

const CANCELLED = Symbol("picker cancelled by user");

function triggerNativeDownload(url, name) {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = name;
  anchor.rel = "noopener";
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
}

function progressReporter(expectedSize, transferIdHint) {
  let lastEmit = 0;
  return (bytes, transferId) => {
    const now = Date.now();
    if (now - lastEmit < 250) return;
    lastEmit = now;
    const size = expectedSize || 0;
    emit("download_progress", {
      id: transferId || transferIdHint,
      bytes,
      percent: size > 0 ? Math.min(100, Math.round((1000 * bytes) / size) / 10) : 0,
    });
  };
}

/**
 * Stream the response to disk in small chunks with a live progress bar.
 * Returns CANCELLED (user closed the picker), true (saved) or null
 * (no picker available — caller should use the native download).
 */
async function streamToDisk(name, size) {
  let handle;
  try {
    handle = await window.showSaveFilePicker({ suggestedName: name });
  } catch (error) {
    if (error?.name === "AbortError") return CANCELLED;
    return null;
  }
  const response = await fetch(`/api/download/${encodeURIComponent(name)}`);
  if (!response.ok) throw new Error(`download failed (${response.status})`);
  const transferId = response.headers.get("X-Transfer-Id");
  const reader = response.body?.getReader();
  if (!reader) {
    // No readable stream (should not happen in modern browsers): hand the
    // URL to the browser instead of buffering anything in JS.
    return null;
  }
  const writer = await handle.createWritable();
  let received = 0;
  const reporter = progressReporter(size, transferId);
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    await writer.write(value); // awaits → backpressure; each chunk is small
    received += value.byteLength;
    reporter(received, transferId);
  }
  await writer.close();
  return true;
}

/** Download one shared file; streams to disk with zero whole-file buffering. */
export async function downloadFile(name, size = 0) {
  const url = `/api/download/${encodeURIComponent(name)}`;

  // Inline progress bar only for big files, and only when we can stream
  // straight to disk (File System Access API).
  if (fsAccessAvailable && size >= PROGRESS_THRESHOLD) {
    try {
      const outcome = await streamToDisk(name, size);
      if (outcome === CANCELLED) return;
      if (outcome === true) {
        notify(`Saved ${name}`);
        return;
      }
      // Picker unavailable/refused → native download below.
    } catch (error) {
      notify(`Download failed: ${error.message}`);
      return;
    }
  }

  // Native path: the browser streams the whole file to disk by itself —
  // zero JS memory, Range/resume supported, works at any size (§11.2).
  triggerNativeDownload(url, name);
}
