/* panel_files.js — Panel 1: server shared folder listing, drag-drop
 * upload zone, per-file upload progress bars, host-only delete. */

import { store, on, send, emit } from "./ws.js";
import { esc, fmtSize, fmtPercent, fmtTime, apiDelete, notify } from "./util.js";
import { uploadFiles, uploads } from "./uploader.js";
import { downloadFile } from "./downloader.js";

function fileIcon(name, mime) {
  if (mime?.startsWith("image/")) return "🖼️";
  if (mime?.startsWith("video/")) return "🎬";
  if (mime?.startsWith("audio/")) return "🎵";
  if (mime === "application/pdf") return "📕";
  if (/\.(zip|rar|7z|tar|gz|bz2|xz)$/i.test(name)) return "📦";
  if (/\.(exe|msi|apk|deb|dmg)$/i.test(name)) return "⚙️";
  if (/\.(txt|md|log|csv|json|xml|py|js|html|css)$/i.test(name)) return "📄";
  return "📃";
}

function renderFiles() {
  const container = document.getElementById("files-list");
  if (!container) return;
  if (!store.files.length) {
    container.innerHTML = '<div class="empty-note">Shared folder is empty — drop files here or onto the SERVER card.</div>';
    return;
  }
  container.innerHTML = store.files
    .map(
      (file) => `
      <div class="file-row" data-name="${esc(file.name)}">
        <div class="file-icon">${fileIcon(file.name, file.mime)}</div>
        <div class="file-name" title="${esc(file.name)}">${esc(file.name)}</div>
        <div class="file-size">${fmtSize(file.size)}</div>
        <div class="file-date">${fmtTime(file.modified)}</div>
        <div class="file-actions">
          <button class="btn btn-sm" data-act="download">⬇ Save</button>
          ${store.isHost ? `<button class="btn btn-danger btn-sm" data-act="delete">🗑</button>` : ""}
        </div>
      </div>`
    )
    .join("");

  container.querySelectorAll(".file-row").forEach((row) => {
    const name = row.dataset.name;
    const size = store.files.find((item) => item.name === name)?.size ?? 0;
    row.querySelector('[data-act="download"]')?.addEventListener("click", () => downloadFile(name, size));
    row.querySelector(".file-name")?.addEventListener("dblclick", () => downloadFile(name, size));
    row.querySelector('[data-act="delete"]')?.addEventListener("click", async () => {
      if (!confirm(`Delete "${name}"?`)) return;
      const result = await apiDelete(`/api/files/${encodeURIComponent(name)}`);
      if (!result.ok) notify(result.data?.error || "delete failed");
    });
  });
}

function renderUploads() {
  const container = document.getElementById("upload-list");
  if (!container) return;
  const entries = [...uploads.values()];
  if (!entries.length) {
    container.innerHTML = "";
    container.classList.add("hidden");
    return;
  }
  container.classList.remove("hidden");
  const phaseLabels = {
    hashing: "computing SHA-256…",
    uploading: "uploading",
    resuming: "resuming",
    repairing: "repairing chunks",
    verifying: "verifying…",
    done: "done ✓",
  };
  container.innerHTML = entries
    .map((upload) => {
      const percent = upload.percent ?? 0;
      let phaseLabel = phaseLabels[upload.phase] ?? esc(upload.phase);
      if (upload.phase === "paused") {
        phaseLabel = `paused — retrying (${esc(upload.error || "network error")})`;
      } else if (upload.phase === "failed") {
        phaseLabel = `failed: ${esc(upload.error || "unknown error")}`;
      }
      const cancellable =
        upload.sessionId && ["uploading", "paused", "resuming"].includes(upload.phase);
      return `
      <div class="upload-row" data-id="${esc(upload.id)}">
        <div class="upload-head">
          <span class="file-name" title="${esc(upload.filename)}">⬆ ${esc(upload.filename)}</span>
          <span class="upload-phase">${phaseLabel}</span>
          <span class="upload-pct">${fmtPercent(percent)}</span>
        </div>
        <div class="progress"><div class="progress-bar ${upload.phase === "failed" ? "bar-danger" : ""}" style="width:${percent}%"></div></div>
        <div class="upload-sub">
          ${fmtSize(Math.round((upload.size * percent) / 100))} / ${fmtSize(upload.size)}
          ${cancellable ? '<button class="btn btn-sm btn-cancel" data-act="cancel-upload">✕ Cancel</button>' : ""}
        </div>
      </div>`;
    })
    .join("");

  container.querySelectorAll('[data-act="cancel-upload"]').forEach((button) => {
    button.addEventListener("click", () => {
      const row = button.closest(".upload-row");
      const upload = row ? uploads.get(row.dataset.id) : null;
      if (upload?.sessionId) {
        send({ type: "cancel_transfer", payload: { transfer_id: upload.sessionId } });
        upload.phase = "failed";
        upload.error = "cancelled — chunks kept for resume";
        emit("uploads-changed");
      }
    });
  });
}

export function initFilesPanel() {
  const dropzone = document.getElementById("dropzone");
  const panel = document.getElementById("panel-files");
  // The dropzone lives inside the panel: bind the panel once so a drop on
  // the dropzone never triggers two uploads (events bubble).
  if (panel) {
    panel.addEventListener("dragover", (event) => {
      event.preventDefault();
      dropzone?.classList.add("drag-over");
    });
    panel.addEventListener("dragleave", (event) => {
      if (!panel.contains(event.relatedTarget)) dropzone?.classList.remove("drag-over");
    });
    panel.addEventListener("drop", async (event) => {
      event.preventDefault();
      dropzone?.classList.remove("drag-over");
      const files = [...(event.dataTransfer?.files ?? [])];
      if (files.length) await uploadFiles(files);
    });
  }

  document.getElementById("pick-file-btn")?.addEventListener("click", () => {
    document.getElementById("file-input")?.click();
  });
  document.getElementById("file-input")?.addEventListener("change", async (event) => {
    const files = [...(event.target.files ?? [])];
    if (files.length) await uploadFiles(files);
    event.target.value = "";
  });

  on("snapshot", renderFiles);
  on("files_changed", renderFiles);
  on("uploads-changed", renderUploads);

  renderFiles();
  renderUploads();
}
