/* panel_devices.js — Panel 2: approved device cards, SERVER card,
 * drag-and-drop targets, "This Device" footer. Pending approvals and
 * kick/block buttons are wired by approval.js. */

import { store, on } from "./ws.js";
import { esc, osIcon, relTime } from "./util.js";
import { uploadFiles } from "./uploader.js";
import { sendToDevice } from "./sender.js";

function deviceCard(device) {
  const isHost = store.isHost;
  const controls = isHost
    ? `<div class="device-actions">
         <button class="btn btn-danger btn-sm" data-action="kick" data-id="${esc(device.id)}">Kick</button>
         <button class="btn btn-danger btn-sm" data-action="block" data-id="${esc(device.id)}">Block</button>
       </div>`
    : "";
  return `
    <div class="device-card drop-target" data-device="${esc(device.id)}" title="Drop files here to send to ${esc(device.name)}">
      <div class="device-icon">${osIcon(device.os)}</div>
      <div class="device-info">
        <div class="device-name">${esc(device.name)}${device.role === "host" ? ' <span class="badge badge-host">host</span>' : ""}</div>
        <div class="device-meta">${esc(device.os)} · ${esc(device.ip)} · ${relTime(device.connected_at)}</div>
      </div>
      ${controls}
    </div>`;
}

function localOs() {
  const ua = navigator.userAgent;
  if (/android/i.test(ua)) return "Android";
  if (/iphone|ipad|ios/i.test(ua)) return "iOS";
  if (/windows/i.test(ua)) return "Windows";
  if (/mac os|macintosh/i.test(ua)) return "macOS";
  if (/linux|x11|termux/i.test(ua)) return "Linux";
  return "Unknown";
}

function render() {
  const list = document.getElementById("devices-list");
  const footer = document.getElementById("this-device");
  if (!list) return;
  list.innerHTML = store.devices.length
    ? store.devices.map(deviceCard).join("")
    : '<div class="empty-note">No other devices yet.</div>';

  if (footer && store.you) {
    footer.innerHTML = `
      <span class="device-icon">${osIcon(localOs())}</span>
      <div class="device-info">
        <div class="device-name">${esc(store.you.name)} <span class="badge badge-you">you</span></div>
        <div class="device-meta">${esc(store.you.role)}${store.you.state === "pending" ? " · awaiting approval" : ""}</div>
      </div>`;
  }

  const online = document.getElementById("online-count");
  if (online) {
    const count = store.devices.length + (store.you?.state === "approved" ? 1 : 0);
    online.textContent = `● ${count} online`;
  }

  list.querySelectorAll("[data-action]").forEach((button) => {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      const id = button.dataset.id;
      const action = button.dataset.action;
      window.dispatchEvent(new CustomEvent("filehub:device-action", { detail: { action, id } }));
    });
  });
}

async function handleDrop(event, targetId) {
  event.preventDefault();
  event.currentTarget.classList.remove("drag-over");
  const files = [...(event.dataTransfer?.files ?? [])];
  if (!files.length) return;
  if (targetId === "server") {
    await uploadFiles(files);
    return;
  }
  for (const file of files) {
    await sendToDevice(targetId, file); // queued sequentially inside sender.js
  }
}

function bindDropTarget(element, targetId) {
  if (!element) return;
  element.addEventListener("dragover", (event) => {
    event.preventDefault();
    event.currentTarget.classList.add("drag-over");
  });
  element.addEventListener("dragleave", (event) => {
    event.currentTarget.classList.remove("drag-over");
  });
  element.addEventListener("drop", (event) => handleDrop(event, targetId));
}

export function initDevicesPanel() {
  bindDropTarget(document.getElementById("server-card"), "server");

  for (const type of ["snapshot", "device_joined", "device_left", "device_kicked", "device_pending", "connection"]) {
    on(type, render);
  }

  // Device cards are re-created on every render: delegate drop handling.
  const list = document.getElementById("devices-list");
  if (list) {
    list.addEventListener("dragover", (event) => {
      const card = event.target.closest(".drop-target");
      if (!card) return;
      event.preventDefault();
      card.classList.add("drag-over");
    });
    list.addEventListener("dragleave", (event) => {
      const card = event.target.closest(".drop-target");
      if (card) card.classList.remove("drag-over");
    });
    list.addEventListener("drop", (event) => {
      const card = event.target.closest(".drop-target");
      if (!card) return;
      handleDrop(event, card.dataset.device);
    });
  }

  render();
}
