/* approval.js — host-only: pending list, approve/reject, kick/block,
 * join-code badge + rotate. Guests never render any of this (§10), and the
 * server independently rejects forged actions with 403 (§12). */

import { store, on } from "./ws.js";
import { esc, osIcon, apiPost, notify } from "./util.js";

const pendingSection = () => document.getElementById("pending-section");
const pendingList = () => document.getElementById("pending-list");

function renderPending() {
  const section = pendingSection();
  const list = pendingList();
  if (!section || !list) return;
  if (!store.isHost) {
    section.classList.add("hidden");
    return;
  }
  section.classList.remove("hidden");
  if (!store.pending.length) {
    list.innerHTML = '<div class="empty-note">Nobody waiting.</div>';
    return;
  }
  list.innerHTML = store.pending
    .map(
      (device) => `
      <div class="pending-card" data-id="${esc(device.id)}">
        <div class="device-icon">${osIcon(device.os)}</div>
        <div class="device-info">
          <div class="device-name">${esc(device.name)}</div>
          <div class="device-meta">${esc(device.os)} · ${esc(device.ip)}</div>
        </div>
        <div class="device-actions">
          <button class="btn btn-accept btn-sm" data-act="approve">✓ Approve</button>
          <button class="btn btn-danger btn-sm" data-act="reject">✗ Reject</button>
        </div>
      </div>`
    )
    .join("");

  list.querySelectorAll(".pending-card").forEach((card) => {
    card.querySelector('[data-act="approve"]')?.addEventListener("click", () =>
      approve(card.dataset.id)
    );
    card.querySelector('[data-act="reject"]')?.addEventListener("click", () =>
      reject(card.dataset.id)
    );
  });
}

function renderCodeBadge() {
  const badge = document.getElementById("join-code-badge");
  const codeEl = document.getElementById("join-code");
  const rotateBtn = document.getElementById("rotate-code-btn");
  if (!badge) return;
  if (store.isHost && store.joinCode) {
    badge.classList.remove("hidden");
    if (codeEl) codeEl.textContent = store.joinCode;
    rotateBtn?.classList.remove("hidden");
  } else {
    badge.classList.add("hidden");
    rotateBtn?.classList.add("hidden");
  }
}

export async function approve(deviceId) {
  const result = await apiPost("/api/auth/approve", { device_id: deviceId, action: "approve" });
  if (!result.ok) notify(result.data?.error || "approve failed");
}

export async function reject(deviceId) {
  const result = await apiPost("/api/auth/approve", { device_id: deviceId, action: "reject" });
  if (!result.ok) notify(result.data?.error || "reject failed");
}

export async function kick(deviceId) {
  const result = await apiPost("/api/auth/kick", { device_id: deviceId });
  if (!result.ok) notify(result.data?.error || "kick failed");
}

export async function block(deviceId) {
  const result = await apiPost("/api/auth/block", { device_id: deviceId });
  if (!result.ok) notify(result.data?.error || "block failed");
}

export async function rotateCode() {
  const result = await apiPost("/api/auth/rotate_code", {});
  if (result.ok && result.data.join_code) {
    store.joinCode = result.data.join_code;
    renderCodeBadge();
    notify("New join code generated");
  } else {
    notify(result.data?.error || "rotate failed");
  }
}

export function initApproval() {
  for (const type of ["snapshot", "device_pending", "device_joined", "device_left", "device_kicked"]) {
    on(type, renderPending);
  }
  on("snapshot", renderCodeBadge);

  window.addEventListener("filehub:device-action", (event) => {
    const { action, id } = event.detail;
    if (action === "kick") {
      if (confirm("Remove this device from the session?")) kick(id);
    } else if (action === "block") {
      if (confirm("Block this device? Its fingerprint will be banned.")) block(id);
    }
  });

  document.getElementById("rotate-code-btn")?.addEventListener("click", rotateCode);

  renderPending();
  renderCodeBadge();
}
