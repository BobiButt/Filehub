/* sendto.js — global "Send a file" flow (mobile-safe). */

import { store } from "./ws.js";

const $ = (id) => document.getElementById(id);
const show = (id, v) => $(id)?.classList.toggle("hidden", !v);

let pendingFile = null;
let pendingTarget = null;
let modalOpen = false;

/* ---------------------------------------------------------------- utils */

function notify(message) {
  window.dispatchEvent(new CustomEvent("filehub:notice", { detail: message }));
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;",
    '"': "&quot;", "'": "&#39;",
  }[c]));
}

function fmtSize(bytes) {
  if (!bytes && bytes !== 0) return "";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0, n = bytes;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return `${n.toFixed(i === 0 ? 0 : 2)} ${units[i]}`;
}

/* ---------------------------------------------------------------- modal */

function openModal() {
  modalOpen = true;
  pendingFile = null;
  pendingTarget = null;

  const input = $("sendto-input");
  if (input) input.value = "";

  const info = $("sendto-file");
  if (info) info.textContent = "No file chosen yet.";

  show("sendto-step-file", true);
  show("sendto-step-target", false);
  show("sendto-go", false);
  show("sendto-modal", true);

  // On mobile, opening the picker programmatically from a *fresh* tap on
  // the "Choose file" button is the only reliable path. So don't auto-open.
}

function closeModal() {
  modalOpen = false;
  show("sendto-modal", false);
  pendingFile = null;
  pendingTarget = null;
}

function gotoStepTarget() {
  if (!pendingFile) { notify("Choose a file first."); return; }
  const nameEl = $("sendto-file-name");
  if (nameEl) {
    nameEl.textContent = `${pendingFile.name} · ${fmtSize(pendingFile.size)}`;
  }
  renderTargets();
  show("sendto-step-file", false);
  show("sendto-step-target", true);
  show("sendto-go", true);
}

function backToFileStep() {
  pendingTarget = null;
  show("sendto-step-target", false);
  show("sendto-step-file", true);
  show("sendto-go", false);
}

/* --------------------------------------------------------------- targets */

function deviceIcon(d) {
  const os = (d.os || d.kind || d.name || "").toLowerCase();
  if (/ios|iphone|ipad|android|mobile|phone/.test(os)) return "📱";
  if (/mac|macos|windows|linux|desktop|pc/.test(os)) return "💻";
  return "💻";
}

function renderTargets() {
  const list = $("sendto-list");
  if (!list) return;
  list.innerHTML = "";

  const youId = store.you?.id;
  const all = Array.isArray(store.devices) ? store.devices : [];

  // Accept either `state` or `status` field — server variants differ.
  const others = all.filter((d) => {
    if (!d || d.id === youId) return false;
    const state = d.state || d.status || "approved";
    return state === "approved";
  });

  const targets = [
    {
      id: "server",
      name: "SERVER (shared folder)",
      meta: "Upload to the server's shared folder",
      icon: "🗄️",
    },
    ...others.map((d) => ({
      id: d.id,
      name: d.name || "Device",
      meta: [d.os || d.kind, d.ip].filter(Boolean).join(" · ") || "Connected device",
      icon: deviceIcon(d),
    })),
  ];

  if (targets.length === 1) {
    const info = document.createElement("div");
    info.style.cssText = "color:var(--fg-dim);font-size:13px;margin-bottom:8px";
    info.textContent =
      "No other devices are connected right now. You can still send to the server.";
    list.appendChild(info);
  }

  for (const t of targets) {
    const el = document.createElement("div");
    el.className = "sendto-item";
    el.dataset.target = t.id;
    el.innerHTML =
      `<span class="sendto-icon">${t.icon}</span>` +
      `<div class="sendto-meta">` +
      `<div class="sendto-name">${escapeHtml(t.name)}</div>` +
      `<div class="sendto-sub">${escapeHtml(t.meta)}</div>` +
      `</div>`;

    // Use pointerup for best cross-device tap response.
    el.addEventListener("click", (ev) => {
      ev.preventDefault();
      ev.stopPropagation();
      selectTarget(t.id);
    });

    list.appendChild(el);
  }

  if (pendingTarget) selectTarget(pendingTarget);
}

function selectTarget(targetId) {
  pendingTarget = targetId;
  const list = $("sendto-list");
  if (!list) return;
  for (const item of list.querySelectorAll(".sendto-item")) {
    item.classList.toggle("selected", item.dataset.target === targetId);
  }
  const goBtn = $("sendto-go");
  if (goBtn) goBtn.disabled = false;
}

/* ------------------------------------------------------------------ send */

async function doSend() {
  if (!pendingFile) { notify("Choose a file first."); return; }
  if (!pendingTarget) { notify("Pick a destination."); return; }

  const file = pendingFile;
  const target = pendingTarget;
  closeModal();

  notify(`Sending ${file.name}…`);

  try {
    if (target === "server") {
      const mod = await import("./uploader.js").catch(() => null);
      if (!mod || typeof mod.uploadFile !== "function") {
        // Fallback: try the files-panel uploader via a custom event.
        window.dispatchEvent(new CustomEvent("filehub:upload", { detail: { file } }));
      } else {
        await mod.uploadFile(file);
      }
      notify(`${file.name} sent to server`);
    } else {
      const mod = await import("./sender.js").catch(() => null);
      if (!mod || typeof mod.sendToDevice !== "function") {
        // Fallback: broadcast via ws layer if it exposes a sender.
        window.dispatchEvent(new CustomEvent("filehub:send", {
          detail: { file, target },
        }));
        notify(`Sending ${file.name} to device…`);
      } else {
        await mod.sendToDevice(target, file);
      }
    }
  } catch (err) {
    console.error("[sendto] failed:", err);
    notify(err?.message || "Send failed");
  }
}

/* ------------------------------------------------------------------ init */

export function initSendToPicker() {
  const globalBtn = $("global-send-btn");
  if (globalBtn) {
    globalBtn.addEventListener("click", (e) => {
      e.preventDefault();
      openModal();
    });
  }

  const pickBtn = $("sendto-pick");
  const input = $("sendto-input");

  if (pickBtn && input) {
    pickBtn.addEventListener("click", (e) => {
      e.preventDefault();
      // The click on a real <button> is a trusted gesture, so .click()
      // on the file input here WILL open the picker on iOS/Android.
      input.click();
    });
  }

  if (input) {
    input.addEventListener("change", (e) => {
      const f = e.target.files?.[0];
      if (!f) return;
      pendingFile = f;
      const info = $("sendto-file");
      if (info) info.textContent = `${f.name} · ${fmtSize(f.size)}`;
      gotoStepTarget();
    });
  }

  $("sendto-change")?.addEventListener("click", (e) => {
    e.preventDefault();
    if (input) input.value = "";
    pendingFile = null;
    backToFileStep();
  });

  $("sendto-cancel")?.addEventListener("click", (e) => {
    e.preventDefault();
    closeModal();
  });

  $("sendto-go")?.addEventListener("click", (e) => {
    e.preventDefault();
    doSend();
  });

  $("sendto-modal")?.addEventListener("click", (e) => {
    if (e.target === e.currentTarget) closeModal();
  });

  // Esc to close
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && modalOpen) closeModal();
  });
}