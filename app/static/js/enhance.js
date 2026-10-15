/* FileHub — front-end enhancements.
 * Pure UI: reads the existing `store` + DOM, calls NO new backend endpoints.
 * Everything here is optional; if a piece fails, the original app keeps working. */

import { store, on } from "/static/js/ws.js";
import { fmtSpeed } from "/static/js/util.js";

const $ = (id) => document.getElementById(id);
const root = document.documentElement;
const notify = (msg) => window.dispatchEvent(new CustomEvent("filehub:notice", { detail: msg }));
const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const finePointer = matchMedia("(hover: hover) and (pointer: fine)").matches;
const isOpen = (id) => { const el = $(id); return !!el && !el.classList.contains("hidden"); };
const setOpen = (id, open) => $(id)?.classList.toggle("hidden", !open);

/* ============================================================ settings */
const KEY = "filehub-ui";
const DEFAULTS = { theme: "ocean", tilt: true, bg: true, compact: false, vibrate: true, wake: true };
const THEMES = { ocean: "#0e1117", aurora: "#0b0a14", sunset: "#120d0d", emerald: "#0a1210", light: "#eef2f8" };
const THEME_ORDER = Object.keys(THEMES);

let ui = { ...DEFAULTS };
try { ui = { ...DEFAULTS, ...JSON.parse(localStorage.getItem(KEY) || "{}") }; } catch { /* ignore */ }
if (!THEMES[ui.theme]) ui.theme = "ocean";

function saveUi() { try { localStorage.setItem(KEY, JSON.stringify(ui)); } catch { /* ignore */ } }

function applyUi() {
  root.dataset.theme = ui.theme;
  root.dataset.fx = ui.tilt ? "on" : "off";
  root.dataset.bg = ui.bg ? "on" : "off";
  root.dataset.density = ui.compact ? "compact" : "cozy";
  const meta = $("meta-theme");
  if (meta) meta.setAttribute("content", THEMES[ui.theme]);
  document.querySelectorAll(".swatch").forEach((s) => {
    const active = s.dataset.theme === ui.theme;
    s.classList.toggle("active", active);
    s.setAttribute("aria-pressed", String(active));
  });
  drawSpark();
}

function syncSwitches() {
  [["opt-tilt", "tilt"], ["opt-bg", "bg"], ["opt-compact", "compact"], ["opt-vibrate", "vibrate"], ["opt-wake", "wake"]]
    .forEach(([id, key]) => {
      const el = $(id);
      if (!el) return;
      el.checked = !!ui[key];
      el.onchange = () => { ui[key] = el.checked; saveUi(); applyUi(); if (key === "wake") syncWake(activeCount()); };
    });
}

document.querySelectorAll(".swatch").forEach((s) =>
  s.addEventListener("click", () => { ui.theme = s.dataset.theme; saveUi(); applyUi(); }));
$("settings-reset")?.addEventListener("click", () => { ui = { ...DEFAULTS }; saveUi(); applyUi(); syncSwitches(); });
$("settings-close")?.addEventListener("click", () => setOpen("settings-modal", false));
$("help-close")?.addEventListener("click", () => setOpen("help-modal", false));
for (const id of ["settings-modal", "help-modal"]) {
  $(id)?.addEventListener("click", (e) => { if (e.target === e.currentTarget) setOpen(id, false); });
}
const openSettings = () => { syncSwitches(); setOpen("settings-modal", true); };
$("settings-btn")?.addEventListener("click", openSettings);
$("nav-settings")?.addEventListener("click", openSettings);
$("help-btn")?.addEventListener("click", () => setOpen("help-modal", true));

const cycleTheme = () => {
  ui.theme = THEME_ORDER[(THEME_ORDER.indexOf(ui.theme) + 1) % THEME_ORDER.length];
  saveUi(); applyUi();
  notify(`🎨 Theme: ${ui.theme}`);
};

/* ===================================================== fullscreen */
const fsBtn = $("fs-btn");
if (fsBtn) {
  if (!document.fullscreenEnabled) fsBtn.classList.add("hidden");
  else fsBtn.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen();
    else root.requestFullscreen().catch(() => {});
  });
}

/* ========================================================== clipboard */
async function copyText(text) {
  try {
    if (navigator.clipboard && window.isSecureContext) { await navigator.clipboard.writeText(text); return true; }
  } catch { /* fall through */ }
  // Plain-HTTP LAN pages have no navigator.clipboard, so use the legacy path.
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.setAttribute("readonly", "");
  ta.style.cssText = "position:fixed;top:0;left:0;opacity:0";
  document.body.appendChild(ta);
  ta.select();
  ta.setSelectionRange(0, text.length);
  let ok = false;
  try { ok = document.execCommand("copy"); } catch { /* ignore */ }
  ta.remove();
  return ok;
}
$("server-url")?.addEventListener("click", async () => {
  const text = $("server-url").textContent.trim();
  if (text) notify((await copyText(text)) ? "🔗 Server address copied" : "Couldn't copy — long-press to select");
});
$("join-code-badge")?.addEventListener("click", async () => {
  const code = $("join-code")?.textContent.trim();
  if (code) notify((await copyText(code)) ? `🔑 Join code ${code} copied` : "Couldn't copy the code");
});

/* ============================================ small DOM watch helper */
function watch(el, fn) {
  if (!el) return;
  let raf = 0;
  const mo = new MutationObserver(() => { if (!raf) raf = requestAnimationFrame(run); });
  function run() {
    raf = 0;
    mo.disconnect(); // our own DOM changes must not retrigger us
    try { fn(); } finally { mo.observe(el, { childList: true, subtree: true }); }
  }
  run();
}

/* ======================================================== files: search + sort */
const filesList = $("files-list");
const search = $("files-search");
const sortSel = $("files-sort");
let fileTotal = 0;

function parseSize(text) {
  const m = /([\d.,]+)\s*(B|KB|MB|GB|TB|KIB|MIB|GIB)?/i.exec(text || "");
  if (!m) return 0;
  const n = parseFloat(m[1].replace(/,/g, ""));
  const unit = (m[2] || "B").toUpperCase().replace("IB", "B");
  return n * 1024 ** ({ B: 0, KB: 1, MB: 2, GB: 3, TB: 4 }[unit] ?? 0);
}

function applyFiles() {
  if (!filesList) return;
  const rows = [...filesList.querySelectorAll(".file-row")];
  rows.forEach((r, i) => { if (!r.dataset.i) r.dataset.i = String(i + 1); }); // remember original order
  const q = (search?.value || "").trim().toLowerCase();
  let shown = 0;
  rows.forEach((r) => {
    const name = (r.querySelector(".file-name")?.textContent || r.textContent).toLowerCase();
    const hit = !q || name.includes(q);
    r.classList.toggle("is-filtered", !hit);
    if (hit) shown++;
  });

  const mode = sortSel?.value || "default";
  const groups = new Map();
  rows.forEach((r) => { if (!groups.has(r.parentNode)) groups.set(r.parentNode, []); groups.get(r.parentNode).push(r); });
  const cmp = {
    default: (a, b) => Number(a.dataset.i) - Number(b.dataset.i),
    name: (a, b) => (a.querySelector(".file-name")?.textContent || "").localeCompare(
      b.querySelector(".file-name")?.textContent || "", undefined, { numeric: true, sensitivity: "base" }),
    size: (a, b) => parseSize(b.querySelector(".file-size")?.textContent) - parseSize(a.querySelector(".file-size")?.textContent),
  }[mode];
  groups.forEach((list, parent) => list.sort(cmp).forEach((r) => parent.appendChild(r)));

  fileTotal = rows.length;
  const count = $("files-count");
  if (count) count.textContent = rows.length ? (q ? `${shown} of ${rows.length}` : `(${rows.length})`) : "";
  filesList.classList.toggle("no-match", !!q && rows.length > 0 && shown === 0);
  refreshStats();
}
search?.addEventListener("input", applyFiles);
sortSel?.addEventListener("change", applyFiles);
search?.addEventListener("keydown", (e) => {
  if (e.key === "Escape") { search.value = ""; applyFiles(); search.blur(); }
});
watch(filesList, applyFiles);

/* =========================================================== traffic filter */
const trafficList = $("traffic-list");
let trafficFilter = "all";

function applyTraffic() {
  if (!trafficList) return;
  trafficList.querySelectorAll(".traffic-row").forEach((r) => {
    const arrow = r.querySelector(".traffic-arrow")?.textContent || r.textContent.slice(0, 12);
    r.classList.toggle("is-filtered", trafficFilter !== "all" && !arrow.includes(trafficFilter));
  });
}
$("traffic-filters")?.addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  trafficFilter = chip.dataset.f;
  document.querySelectorAll("#traffic-filters .chip").forEach((c) => c.classList.toggle("active", c === chip));
  applyTraffic();
});
watch(trafficList, applyTraffic);

const trafficPanel = $("panel-traffic");
const trafficToggle = $("traffic-toggle");
function setTrafficCollapsed(collapsed) {
  trafficPanel?.classList.toggle("collapsed", collapsed);
  trafficToggle?.setAttribute("aria-expanded", String(!collapsed));
  try { localStorage.setItem("filehub-traffic-collapsed", collapsed ? "1" : "0"); } catch { /* ignore */ }
}
trafficToggle?.addEventListener("click", () => setTrafficCollapsed(!trafficPanel.classList.contains("collapsed")));
try { if (localStorage.getItem("filehub-traffic-collapsed") === "1") setTrafficCollapsed(true); } catch { /* ignore */ }

/* ============================================================ stats + spark */
const speeds = new Map(); // transfer id -> { bps, t }
on("transfer_progress", (p) => { if (p && p.id != null) speeds.set(p.id, { bps: p.speed_bps || 0, t: Date.now() }); });

const SAMPLES = 40;
const samples = new Array(SAMPLES).fill(0);

function activeCount() {
  let n = 0;
  if (store.transfers?.values) for (const t of store.transfers.values()) if (["active", "paused"].includes(t.status)) n++;
  return n;
}
function totalSpeed() {
  const now = Date.now();
  let sum = 0;
  for (const [id, s] of speeds) { if (now - s.t > 3000) speeds.delete(id); else sum += s.bps; }
  return sum;
}
function pendingCount() { return $("pending-list")?.children.length || 0; }

function drawSpark() {
  const c = $("spark");
  if (!c) return;
  const r = c.getBoundingClientRect();
  if (!r.width) return;
  const dpr = window.devicePixelRatio || 1;
  const w = Math.round(r.width * dpr), h = Math.round(r.height * dpr);
  if (c.width !== w || c.height !== h) { c.width = w; c.height = h; }
  const g = c.getContext("2d");
  g.clearRect(0, 0, w, h);
  const max = Math.max(...samples, 1);
  const step = w / (SAMPLES - 1);
  const color = getComputedStyle(root).getPropertyValue("--accent").trim() || "#4a9eff";
  g.beginPath();
  samples.forEach((v, i) => {
    const x = i * step, y = h - (v / max) * h * 0.7 - 3 * dpr;
    if (i) g.lineTo(x, y); else g.moveTo(x, y);
  });
  g.strokeStyle = color; g.lineWidth = 2 * dpr; g.lineJoin = "round"; g.stroke();
  g.lineTo(w, h); g.lineTo(0, h); g.closePath();
  g.globalAlpha = 0.18; g.fillStyle = color; g.fill(); g.globalAlpha = 1;
}

function refreshStats() {
  const devices = (store.devices?.length || 0) + (store.you?.state === "approved" ? 1 : 0);
  const active = activeCount();
  const speed = totalSpeed();
  if ($("stat-files")) $("stat-files").textContent = String(fileTotal);
  if ($("stat-devices")) $("stat-devices").textContent = String(devices);
  if ($("stat-active")) $("stat-active").textContent = String(active);
  if ($("stat-speed")) $("stat-speed").textContent = speed > 0 ? fmtSpeed(speed) : "Idle";
  $("stat-active-card")?.classList.toggle("live", active > 0);
  $("stat-speed-card")?.classList.toggle("live", speed > 0);

  const tb = $("nav-traffic-badge");
  if (tb) { tb.textContent = String(active); tb.classList.toggle("hidden", active === 0); }
  updateTitle(active);
}

function updateTitle(active = activeCount()) {
  const p = pendingCount();
  document.title = `${p ? `(${p}) ` : ""}${active ? "⇅ " : ""}FileHub`;
}

["snapshot", "scheduler_update", "device_joined", "device_left", "device_kicked", "transfer_done", "transfer_failed", "transfer_progress"]
  .forEach((e) => on(e, refreshStats));

setInterval(() => {
  if (document.hidden) return;
  samples.push(totalSpeed());
  samples.shift();
  drawSpark();
  refreshStats();
  syncWake(activeCount());
}, 1000);
addEventListener("resize", drawSpark);

/* ============================================ pending-approval awareness */
let lastPending = 0;
watch($("pending-list"), () => {
  const n = pendingCount();
  const badge = $("nav-devices-badge");
  if (badge) { badge.textContent = String(n); badge.classList.toggle("hidden", n === 0); }
  if (n > lastPending) notify("🔔 A device is waiting for your approval");
  lastPending = n;
  updateTitle();
});

/* vibrate when an incoming-file dialog opens */
const relayModal = $("relay-modal");
if (relayModal) {
  let wasHidden = relayModal.classList.contains("hidden");
  new MutationObserver(() => {
    const hidden = relayModal.classList.contains("hidden");
    if (wasHidden && !hidden && ui.vibrate && navigator.vibrate) navigator.vibrate([120, 60, 120]);
    wasHidden = hidden;
  }).observe(relayModal, { attributes: true, attributeFilter: ["class"] });
}

/* ================================================ keep screen awake */
let wake = null;
async function syncWake(active) {
  if (!("wakeLock" in navigator)) return;
  try {
    if (active > 0 && ui.wake && !wake && document.visibilityState === "visible") {
      wake = await navigator.wakeLock.request("screen");
      wake.addEventListener("release", () => { wake = null; });
    } else if ((active === 0 || !ui.wake) && wake) {
      await wake.release();
      wake = null;
    }
  } catch { /* unsupported or insecure context — ignore */ }
}
document.addEventListener("visibilitychange", () => syncWake(activeCount()));

/* ===================================================== mobile tabs */
const TABS = ["files", "devices", "traffic"];
function setTab(tab) {
  if (!TABS.includes(tab)) tab = "files";
  document.body.dataset.tab = tab;
  document.querySelectorAll("#mobile-nav [data-tab]").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  try { localStorage.setItem("filehub-tab", tab); } catch { /* ignore */ }
}
document.querySelectorAll("#mobile-nav [data-tab]").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));
$("nav-send")?.addEventListener("click", () => $("global-send-btn")?.click());
try { setTab(localStorage.getItem("filehub-tab") || "files"); } catch { setTab("files"); }

/* ================================== 3D tilt + background parallax (mouse only) */
const TILT_SEL = ".stat, .device-card";
let tilted = null;
const tiltAllowed = () => ui.tilt && finePointer && !reduceMotion;
function resetTilt(el) {
  if (!el) return;
  el.classList.remove("is-tilting");
  ["--rx", "--ry", "--mx", "--my"].forEach((p) => el.style.removeProperty(p));
}
let moveRaf = 0;
addEventListener("pointermove", (e) => {
  if (e.pointerType === "touch" || !finePointer || reduceMotion) return;
  if (moveRaf) return;
  moveRaf = requestAnimationFrame(() => {
    moveRaf = 0;
    const scene = $("scene");
    if (scene && root.dataset.bg !== "off") {
      scene.style.setProperty("--px", ((e.clientX / innerWidth) * 2 - 1).toFixed(3));
      scene.style.setProperty("--py", ((e.clientY / innerHeight) * 2 - 1).toFixed(3));
    }
    // panel glare follows the cursor
    const panel = e.target.closest?.(".panel");
    if (panel) {
      const pr = panel.getBoundingClientRect();
      panel.style.setProperty("--mx", `${e.clientX - pr.left}px`);
      panel.style.setProperty("--my", `${e.clientY - pr.top}px`);
    }
    if (!tiltAllowed()) { if (tilted) { resetTilt(tilted); tilted = null; } return; }
    const el = e.target.closest?.(TILT_SEL);
    if (tilted && tilted !== el) { resetTilt(tilted); tilted = null; }
    if (!el) return;
    const r = el.getBoundingClientRect();
    const x = (e.clientX - r.left) / r.width, y = (e.clientY - r.top) / r.height;
    const max = el.classList.contains("stat") ? 8 : 5;
    el.style.setProperty("--ry", `${((x - 0.5) * 2 * max).toFixed(2)}deg`);
    el.style.setProperty("--rx", `${(-(y - 0.5) * 2 * max).toFixed(2)}deg`);
    el.style.setProperty("--mx", `${(x * 100).toFixed(1)}%`);
    el.style.setProperty("--my", `${(y * 100).toFixed(1)}%`);
    el.classList.add("is-tilting");
    tilted = el;
  });
}, { passive: true });
document.addEventListener("pointerout", (e) => { if (!e.relatedTarget && tilted) { resetTilt(tilted); tilted = null; } });

/* ====================== drag-over hint, paste-to-upload, friendly device name */
let dragDepth = 0;
const hasFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
addEventListener("dragenter", (e) => { if (hasFiles(e)) { dragDepth++; document.body.classList.add("dragging"); } });
addEventListener("dragleave", (e) => { if (hasFiles(e) && --dragDepth <= 0) { dragDepth = 0; document.body.classList.remove("dragging"); } });
["drop", "dragend"].forEach((t) => addEventListener(t, () => { dragDepth = 0; document.body.classList.remove("dragging"); }, true));

const typing = () => /^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement?.tagName || "") || document.activeElement?.isContentEditable;
const gated = () => isOpen("join-overlay") || isOpen("waiting-overlay") || isOpen("notice-overlay");

document.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  const input = $("file-input");
  if (!files.length || !input || gated() || typing()) return;
  e.preventDefault();
  const dt = new DataTransfer();
  files.forEach((f) => dt.items.add(f.name === "image.png" ? new File([f], `pasted-${Date.now()}.png`, { type: f.type }) : f));
  input.files = dt.files;
  input.dispatchEvent(new Event("change", { bubbles: true })); // same path as the normal picker
  notify(`📋 Uploading ${files.length} pasted file${files.length > 1 ? "s" : ""}…`);
});

const nameInput = $("join-name");
if (nameInput && !nameInput.value) {
  const ua = navigator.userAgent;
  const kind = /iPhone/.test(ua) ? "iPhone" : /iPad/.test(ua) ? "iPad"
    : /Android/.test(ua) ? (/Mobile/.test(ua) ? "Android Phone" : "Android Tablet")
    : /Windows/.test(ua) ? "Windows PC" : /Mac/.test(ua) ? "Mac" : /Linux/.test(ua) ? "Linux PC" : "Device";
  nameInput.value = `${kind} ${Math.floor(10 + Math.random() * 90)}`;
}

/* ============================================================ shortcuts */
addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    if (isOpen("help-modal")) return setOpen("help-modal", false);
    if (isOpen("settings-modal")) return setOpen("settings-modal", false);
    if (isOpen("qr-modal")) return $("qr-close")?.click();
    if (isOpen("sendto-modal")) return $("sendto-cancel")?.click();
    return;
  }
  if (e.ctrlKey || e.metaKey || e.altKey || typing() || gated()) return;
  switch (e.key) {
    case "/": e.preventDefault(); if (getComputedStyle($("panel-files")).display === "none") setTab("files"); search?.focus(); break;
    case "u": case "U": $("pick-file-btn")?.click(); break;
    case "s": case "S": $("global-send-btn")?.click(); break;
    case "q": case "Q": $("qr-btn")?.click(); break;
    case "t": case "T": cycleTheme(); break;
    case "?": setOpen("help-modal", true); break;
    case "1": case "2": case "3":
      if (getComputedStyle($("mobile-nav")).display !== "none") setTab(TABS[Number(e.key) - 1]);
      break;
    default: break;
  }
});

/* ================================================================ boot */
applyUi();
syncSwitches();
refreshStats();