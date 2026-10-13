/* sender.js — client → client relay sender (§11.3 steps 1, 4-6, 8).
 *
 * Queue: one outgoing relay at a time (each already uses up to your_lanes
 * worth of the budget conceptually; the server enforces the cap). Chunks
 * flow as binary frames with TCP backpressure via ws.bufferedAmount.
 */

import { on, send, sendBytes, bufferedAmount, store, emit } from "./ws.js";
import { buildRelayFrame, crc32, sha256Blob, sleep } from "./util.js";

const MAX_WAIT_MS = 120_000;

const queue = [];
let active = null; // { transferId, resolve, reject, timer, aborted }

function clearActive(entry) {
  if (active === entry) active = null;
}

function settle(kind, payload) {
  if (!active) return;

  if (kind === "accept") {
    if (active.transferId !== null) return; // already answered
    clearTimeout(active.timer);
    active.transferId = payload.transfer_id ?? null;
    active.resolve(active);
    return;
  }

  // reject/abort apply to the outstanding offer or the running transfer
  if (active.transferId !== null && payload.transfer_id !== active.transferId) return;

  clearTimeout(active.timer);
  const entry = active;
  active = null;
  entry.aborted = true;
  entry.reject(new Error(payload.reason || "declined"));
}

export function initSender() {
  on("relay_accepted", (payload) => settle("accept", payload));
  on("relay_rejected", (payload) => settle("reject", payload));
  on("relay_aborted", (payload) => settle("abort", payload));
  on("error", (payload) => {
    if (payload?.context === "offer_relay" && active && active.transferId === null) {
      clearTimeout(active.timer);
      const entry = active;
      active = null;
      entry.reject(new Error(payload.error));
    }
  });
}

/** Ask the server to create an offer, then wait for the receiver's answer. */
function offer(toDevice, file, sha256) {
  return new Promise((resolve, reject) => {
    const entry = {
      transferId: null,
      resolve,
      reject,
      timer: null,
      aborted: false,
    };
    entry.timer = setTimeout(() => {
      clearActive(entry);
      reject(new Error("no response from the other device"));
    }, MAX_WAIT_MS);
    active = entry;

    const sent = send({
      type: "offer_relay",
      payload: {
        to_device: toDevice,
        filename: file.name,
        size: file.size,
        sha256,
      },
    });
    if (!sent) {
      clearTimeout(entry.timer);
      clearActive(entry);
      reject(new Error("not connected"));
    }
  });
}

/** Stream every chunk as a binary frame, then send finish_relay. */
async function streamFile(entry, transferId, file) {
  const chunkSize = window.__FILEHUB__?.chunkSize || 1048576;
  const total = file.size === 0 ? 0 : Math.ceil(file.size / chunkSize);

  for (let index = 0; index < total; index++) {
    if (entry.aborted) throw new Error("transfer cancelled");
    const start = index * chunkSize;
    const end = Math.min(start + chunkSize, file.size);
    const buffer = await file.slice(start, end).arrayBuffer();
    const payload = new Uint8Array(buffer);
    const frame = buildRelayFrame(transferId, index, crc32(payload), payload);
    sendBytes(frame);

    // Backpressure: wait until the socket buffer drains before continuing.
    while (bufferedAmount() > 8 * chunkSize) {
      if (entry.aborted) throw new Error("transfer cancelled");
      await sleep(10);
    }
  }

  if (entry.aborted) throw new Error("transfer cancelled");
  send({ type: "finish_relay", payload: { transfer_id: transferId } });
}

async function runTransfer(deviceId, file) {
  emit("notice", `Hashing ${file.name}…`);
  const sha256 = await sha256Blob(file);
  const entry = await offer(deviceId, file, sha256);
  try {
    emit("notice", `Sending ${file.name}…`);
    await streamFile(entry, entry.transferId, file);
  } finally {
    clearActive(entry);
  }
}

async function pump() {
  if (active || !queue.length) return;
  const job = queue.shift();
  try {
    await runTransfer(job.deviceId, job.file);
    emit("notice", `Sent ${job.file.name}`);
    job.resolve(true);
  } catch (error) {
    emit("notice", `${job.file.name}: ${error.message}`);
    job.resolve(false);
  } finally {
    pump();
  }
}

/** Queue a file for sending to another device (safe to call repeatedly). */
export function sendToDevice(deviceId, file) {
  return new Promise((resolve) => {
    queue.push({ deviceId, file, resolve });
    pump();
  });
}