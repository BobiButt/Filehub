/* panel_traffic.js — Live Traffic panel.
 *
 * Renders every transfer lifecycle event into #traffic-list.
 * Subscribes to:
 *   - "traffic"              (single TrafficEvent)
 *   - "transfer_start"       (new TransferState)
 *   - "transfer_progress"    ({ id, bytes, percent, speed_bps })
 *   - "transfer_done"        (final TransferState)
 *   - "transfer_failed"      ({ id, error })
 *
 * Each row is keyed by transfer_id so progress updates the same row.
 */

import { on, store } from "./ws.js";

const MAX_ROWS = 200;
const rows = new Map(); // transfer_id → DOM element
let listEl = null;

function fmtTime(d = new Date()) {
  return d.toTimeString().slice(0, 8);
}

function fmtSize(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  let i = 0;
  let n = bytes;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i++;
  }
  return `${n.toFixed(n >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function arrow(kind) {
  if (kind === "upload") return "⬆";
  if (kind === "download") return "⬇";
  if (kind === "relay") return "➡";
  return "·";
}

function textColor(kind) {
  if (kind === "upload") return "#4a9eff";
  if (kind === "download") return "#3fb950";
  if (kind === "relay") return "#d29922";
  return "#9aa4b2";
}

function deviceName(id) {
  if (!id || id === "server") return "SERVER";
  const d = store.devices.find((x) => x.id === id);
  return d?.name || id.slice(0, 8);
}

/** Build a traffic row element from a raw event-like object. */
function buildRow(ev) {
  const el = document.createElement("div");
  el.className = "traffic-row";

  const arrowSymbol = arrow(ev.kind);
  const color = textColor(ev.kind);

  const status = ev.status || "active";
  const statusText =
    status === "active"
      ? ev.percent != null
        ? `${Math.floor(ev.percent)}%`
        : "active"
      : status;
  const statusColor =
    status === "failed" || status === "rejected"
      ? "#f85149"
      : status === "done"
      ? "#3fb950"
      : "#9aa4b2";

  el.innerHTML =
    `<span class="traffic-time">${fmtTime()}</span>` +
    `<span class="traffic-arrow" style="color:${color}">${arrowSymbol}</span>` +
    `<span class="traffic-main">` +
    `  <b>${escapeHtml(deviceName(ev.sender_id))}</b>` +
    `  → <b>${escapeHtml(deviceName(ev.receiver_id))}</b>` +
    (ev.filename ? ` · ${escapeHtml(ev.filename)}` : "") +
    (ev.size ? ` · ${fmtSize(ev.size)}` : "") +
    `</span>` +
    `<span class="traffic-status" style="color:${statusColor}">${escapeHtml(
      statusText
    )}</span>`;

  return el;
}

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#39;",
  }[c]));
}

function prependRow(ev) {
  if (!listEl) return;
  const el = buildRow(ev);
  listEl.prepend(el);

  // Trim old rows.
  while (listEl.children.length > MAX_ROWS) {
    listEl.removeChild(listEl.lastChild);
  }

  return el;
}

function updateRow(transferId, updates) {
  const el = rows.get(transferId);
  if (!el) return;
  const statusEl = el.querySelector(".traffic-status");
  if (!statusEl) return;
  if (updates.percent != null) {
    statusEl.textContent = `${Math.floor(updates.percent)}%`;
    statusEl.style.color = "#9aa4b2";
  } else if (updates.status) {
    statusEl.textContent = updates.status;
    statusEl.style.color =
      updates.status === "done"
        ? "#3fb950"
        : updates.status === "failed"
        ? "#f85149"
        : "#9aa4b2";
  }
}

/* ------------------------------------------------------------------ init */

export function initTrafficPanel() {
  listEl = document.getElementById("traffic-list");
  if (!listEl) return;

  // New transfer starts → create row + remember it.
  on("transfer_start", (state) => {
    const el = prependRow({
      kind: state.kind,
      sender_id: state.sender_id,
      receiver_id: state.receiver_id,
      filename: state.filename,
      size: state.size,
      status: state.status || "active",
      percent: 0,
    });
    if (el && state.id) rows.set(state.id, el);
  });

  // Single traffic events (login, join, kick, log messages).
  on("traffic", (ev) => {
    // If it's a one-shot event (auth, admin), just prepend.
    if (ev.kind === "auth" || ev.kind === "admin") {
      const el = prependRow({
        kind: "auth",
        sender_id: ev.actor,
        receiver_id: ev.target,
        filename: ev.filename || "",
        size: 0,
        status: ev.status || "ok",
      });
      return;
    }
    if (ev.transfer_id && rows.has(ev.transfer_id)) {
      updateRow(ev.transfer_id, {
        percent: ev.percent,
        status: ev.status,
      });
      return;
    }
    const el = prependRow(ev);
    if (el && ev.transfer_id) rows.set(ev.transfer_id, el);
  });

  // Progress updates.
  on("transfer_progress", (p) => {
    updateRow(p.id, { percent: p.percent });
  });

  on("transfer_done", (state) => {
    updateRow(state.id, { status: "done", percent: 100 });
  });

  on("transfer_failed", (p) => {
    updateRow(p.id, { status: "failed" });
  });
}