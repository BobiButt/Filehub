/* receiver.js — client → client relay receiver with the offer modal (§11.3).

Accept → stream chunks to disk via the File System Access API when
available (hashing incrementally in the worker); otherwise buffer in
memory and save as a Blob at the end — with a warning for large files. */

import { on, onBinary, send, store } from "./ws.js";
import { createHasher, crc32, esc, fmtSize, notify, parseRelayFrame } from "./util.js";

const fsAccessAvailable = typeof window.showSaveFilePicker === "function";
const queue = []; // pending offers, one modal at a time
let current = null;
let writeChain = Promise.resolve();
const receivers = new Map(); // transferId -> active incoming session

function modalElements() {
  return {
    modal: document.getElementById("relay-modal"),
    message: document.getElementById("relay-message"),
    accept: document.getElementById("relay-accept"),
    decline: document.getElementById("relay-decline"),
    progress: document.getElementById("relay-progress"),
  };
}

function showOffer(offer) {
  const { modal, message, progress, accept, decline } = modalElements();
  if (!modal) return;
  const memoryWarning =
    !fsAccessAvailable && offer.size > 512 * 1024 * 1024
      ? `<div class="warning">⚠️ This browser cannot stream to disk — ` +
        `${fmtSize(offer.size)} will be buffered in memory before saving.</div>`
      : "";
  message.innerHTML =
    `<strong>${esc(offer.fromName)}</strong> wants to send ` +
    `<strong>${esc(offer.filename)}</strong> (${fmtSize(offer.size)}).${memoryWarning}`;
  accept?.classList.remove("hidden");
  decline?.classList.remove("hidden");
  if (progress) {
    progress.classList.add("hidden");
    progress.querySelector(".progress-bar").style.width = "0%";
  }
  modal.classList.remove("hidden");
}

function hideModal() {
  modalElements().modal?.classList.add("hidden");
}

function nextQueued() {
  current = null;
  const next = queue.shift();
  if (next) showOffer(next);
}

function acceptOffer(offer) {
  return new Promise((resolve, reject) => {
    const transferId = offer.transferId;
    const session = {
      transferId,
      filename: offer.filename,
      size: offer.size,
      expectedSha: offer.sha256,
      bytes: 0,
      hasher: null,
      sink: null, // { write(bytes), close() } or memory mode
      chunks: [],
      done: null,
      fail: null,
      resolve,
      reject,
    };
    receivers.set(transferId, session);
    prepareSink(session)
      .then(() => {
        // Sink (save picker + writer) is ready: ONLY now may the server
        // release the sender. Sending accept_relay earlier lets the first
        // frames arrive while session.sink is still null → crash (§11.3).
        send({ type: "accept_relay", payload: { transfer_id: transferId } });
        resolve(session);
      })
      .catch((error) => {
        receivers.delete(transferId);
        send({ type: "reject_relay", payload: { transfer_id: transferId, reason: error.message } });
        reject(error);
      });
  });
}

async function prepareSink(session) {
  session.hasher = createHasher();
  await session.hasher.start();
  if (fsAccessAvailable) {
    let handle;
    try {
      // Must run inside the Accept click's user activation.
      handle = await window.showSaveFilePicker({ suggestedName: session.filename });
    } catch (error) {
      if (error?.name === "AbortError") throw new Error("save location not chosen");
      throw error;
    }
    const writer = await handle.createWritable();
    session.sink = {
      async write(bytes) {
        await writer.write(bytes);
      },
      async close() {
        await writer.close();
      },
    };
  } else {
    session.sink = {
      async write(bytes) {
        session.chunks.push(bytes);
      },
      async close() {
        const blob = new Blob(session.chunks, { type: "application/octet-stream" });
        const url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url;
        anchor.download = session.filename;
        document.body.appendChild(anchor);
        anchor.click();
        anchor.remove();
        setTimeout(() => URL.revokeObjectURL(url), 60_000);
        session.chunks = [];
      },
    };
  }
}

/* ---------------------------------------------------- frame handling --- */

function updateProgress(session) {
  const { progress } = modalElements();
  if (!progress || current?.transferId !== session.transferId) return;
  progress.classList.remove("hidden");
  const percent = session.size > 0 ? Math.min(100, (1000 * session.bytes) / session.size / 10) : 100;
  progress.querySelector(".progress-bar").style.width = `${percent}%`;
  progress.querySelector(".progress-label").textContent =
    `${fmtSize(session.bytes)} / ${fmtSize(session.size)}`;
}

function handleBinaryFrame(buffer) {
  let frame;
  try {
    frame = parseRelayFrame(buffer);
  } catch (error) {
    console.warn("bad relay frame", error);
    return;
  }
  const session = receivers.get(frame.transferId);
  if (!session) return;
  if ((crc32(frame.payload) >>> 0) !== frame.checksum) {
    // Server already verifies CRCs; local check is defense in depth.
    send({
      type: "cancel_transfer",
      payload: { transfer_id: frame.transferId, reason: "crc32 mismatch at receiver" },
    });
    return;
  }
  const payload = frame.payload;
  session.bytes += payload.length;
  // Serialize writes through a promise chain (onmessage cannot await).
  writeChain = writeChain
    .then(async () => {
      await session.sink.write(payload);
      await session.hasher.update(payload);
    })
    .catch((error) => failSession(session, error.message));
  updateProgress(session);
}

function failSession(session, reason) {
  receivers.delete(session.transferId);
  send({
    type: "cancel_transfer",
    payload: { transfer_id: session.transferId, reason },
  });
  notify(`${session.filename}: ${reason}`);
  hideModal();
  nextQueued();
}

async function finalize(session) {
  try {
    await writeChain;
    if (session.bytes !== session.size) {
      throw new Error(`received ${session.bytes} of ${session.size} bytes`);
    }
    const digest = await session.hasher.finish();
    await session.sink.close();
    if (digest !== session.expectedSha) {
      send({
        type: "cancel_transfer",
        payload: { transfer_id: session.transferId, reason: "sha256 mismatch" },
      });
      throw new Error("SHA-256 mismatch — the file was corrupted in transit");
    }
    receivers.delete(session.transferId);
    notify(`Received ${session.filename} (${fmtSize(session.size)})`);
    hideModal();
    nextQueued();
  } catch (error) {
    failSession(session, error.message);
  }
}

/* ------------------------------------------------------ modal wiring --- */

export function initReceiver() {
  on("relay_offer", (payload) => {
    const offer = {
      transferId: payload.transfer_id,
      fromName: payload.from_device?.name ?? "Another device",
      filename: payload.filename,
      size: payload.size,
      sha256: payload.sha256,
    };
    if (current) {
      queue.push(offer);
      return;
    }
    current = offer;
    showOffer(offer);
  });

  on("relay_finished", async (payload) => {
    const session = receivers.get(payload.transfer_id);
    if (session) await finalize(session);
  });

  on("relay_aborted", (payload) => {
    const session = receivers.get(payload.transfer_id);
    if (session) {
      receivers.delete(payload.transfer_id);
      notify(`${session.filename}: ${payload.reason}`);
    }
    if (current?.transferId === payload.transfer_id) {
      hideModal();
      nextQueued();
    }
  });

  on("snapshot", () => {
    // After a reconnect the server aborts unseen relays: close a stale modal.
    if (current && !receivers.has(current.transferId) && !store.transfers.has(current.transferId)) {
      hideModal();
      nextQueued();
    }
  });

  onBinary(handleBinaryFrame);

  const { accept, decline } = modalElements();
  accept?.addEventListener("click", async () => {
    if (!current) return;
    const offer = current;
    accept.classList.add("hidden");
    decline?.classList.add("hidden");
    try {
      await acceptOffer(offer);
    } catch (error) {
      notify(error.message);
      hideModal();
      nextQueued();
    }
  });
  decline?.addEventListener("click", () => {
    if (!current) return;
    send({
      type: "reject_relay",
      payload: { transfer_id: current.transferId, reason: "declined" },
    });
    hideModal();
    nextQueued();
  });
}
