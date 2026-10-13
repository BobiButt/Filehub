/* ws.js — WebSocket client + reconnect + heartbeat + shared client store.

One socket carries presence, dashboard state, relay signalling and
progress (§7). Incoming messages update the store first, then notify
module listeners through a single event bus. */

export const store = {
  you: null,            // { id, name, role, state }
  localName: "",        // name used for hello before the first snapshot
  isHost: false,
  joinCode: null,
  devices: [],          // approved devices (excluding self)
  pending: [],          // host only
  files: [],
  traffic: [],
  transfers: new Map(), // id -> TransferState
  scheduler: { global_budget: 18, active_streams: 0, your_lanes: 4 },
  online: false,
  stopped: false,       // set after kick/reject: no auto-reconnect
};

const listeners = new Map();
let socket = null;
let reconnectDelay = 500;
let reconnectTimer = null;

/* ----------------------------------------------------------- event bus --- */

export function on(type, callback) {
  if (!listeners.has(type)) listeners.set(type, []);
  listeners.get(type).push(callback);
  return () => {
    const list = listeners.get(type) ?? [];
    const index = list.indexOf(callback);
    if (index >= 0) list.splice(index, 1);
  };
}

export function emit(type, payload) {
  for (const callback of listeners.get(type) ?? []) {
    try {
      callback(payload);
    } catch (error) {
      console.error(`listener for "${type}" failed`, error);
    }
  }
}

/* ------------------------------------------------------------ sending --- */

export function send(message) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return false;
  socket.send(JSON.stringify(message));
  return true;
}

export function sendBytes(buffer) {
  if (!socket || socket.readyState !== WebSocket.OPEN) {
    throw new Error("connection lost");
  }
  socket.send(buffer);
  return true;
}

export function bufferedAmount() {
  return socket ? socket.bufferedAmount : 0;
}

export function stopReconnecting() {
  store.stopped = true;
  clearTimeout(reconnectTimer);
  if (socket) {
    try {
      socket.close();
    } catch { /* already closing */ }
  }
}

/* ------------------------------------------------------ store updates --- */

function applySnapshot(payload) {
  store.you = payload.you;
  store.isHost = payload.you?.role === "host";
  if (payload.you?.name) store.localName = payload.you.name;
  store.devices = payload.devices ?? [];
  store.pending = payload.pending ?? [];
  store.files = payload.files ?? [];
  store.traffic = payload.traffic ?? [];
  store.transfers = new Map((payload.transfers ?? []).map((t) => [t.id, t]));
  store.scheduler = payload.scheduler ?? store.scheduler;
  emit("snapshot", payload);
}

function upsert(list, device) {
  const index = list.findIndex((item) => item.id === device.id);
  if (index >= 0) list[index] = device;
  else list.push(device);
}

function removeById(list, id) {
  const index = list.findIndex((item) => item.id === id);
  if (index >= 0) list.splice(index, 1);
}

function handleMessage(data) {
  const type = data.type;
  const payload = data.payload ?? {};
  switch (type) {
    case "snapshot":
      applySnapshot(payload);
      break;
    case "ping":
      send({ type: "pong" });
      break;
    case "device_pending":
      upsert(store.pending, payload);
      emit(type, payload);
      break;
    case "device_joined":
      removeById(store.pending, payload.id);
      if (!store.you || payload.id !== store.you.id) upsert(store.devices, payload);
      emit(type, payload);
      break;
    case "device_left":
      removeById(store.devices, payload.device_id);
      removeById(store.pending, payload.device_id);
      emit(type, payload);
      break;
    case "device_kicked":
      removeById(store.devices, payload.device_id);
      removeById(store.pending, payload.device_id);
      if (store.you && payload.device_id === store.you.id) {
        store.stopped = true;
        emit("you_kicked", payload.reason || "removed");
      }
      emit(type, payload);
      break;
    case "files_changed":
      store.files = payload.files ?? [];
      emit(type, payload);
      break;
    case "traffic":
      store.traffic.push(payload);
      if (store.traffic.length > 200) store.traffic.splice(0, store.traffic.length - 200);
      emit(type, payload);
      break;
    case "transfer_start":
      store.transfers.set(payload.id, payload);
      emit(type, payload);
      break;
    case "transfer_progress": {
      const transfer = store.transfers.get(payload.id);
      if (transfer) {
        transfer.bytes_transferred = payload.bytes;
        emit(type, { ...payload, transfer });
      } else {
        emit(type, payload);
      }
      break;
    }
    case "transfer_done":
      store.transfers.delete(payload.id);
      emit(type, payload);
      break;
    case "transfer_failed":
      store.transfers.delete(payload.id);
      emit(type, payload);
      break;
    case "scheduler_update":
      store.scheduler = payload;
      emit(type, payload);
      break;
    default:
      emit(type, payload); // relay_*, error, anything future
  }
}

/* ---------------------------------------------------------- connection --- */

const binaryHandlers = [];
export function onBinary(callback) {
  binaryHandlers.push(callback);
}

function notifyBinary(buffer) {
  for (const callback of binaryHandlers) {
    try {
      callback(buffer);
    } catch (error) {
      console.error("binary handler failed", error);
    }
  }
}

function scheduleReconnect() {
  if (store.stopped) return;
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(() => {
    if (!store.stopped) open();
  }, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 2, 8000);
}

export function connect(name) {
  if (name) store.localName = name;
  store.stopped = false;
  reconnectDelay = 500;
  open();
}

function open() {
  if (store.stopped) return;
  const protocol = location.protocol === "https:" ? "wss:" : "ws:";
  socket = new WebSocket(`${protocol}//${location.host}/ws`);
  socket.binaryType = "arraybuffer";

  socket.onopen = () => {
    store.online = true;
    reconnectDelay = 500;
    emit("connection", { online: true });
    send({ type: "hello", payload: { name: store.localName } });
  };

  socket.onmessage = (event) => {
    lastInbound = Date.now();
    if (event.data instanceof ArrayBuffer) {
      notifyBinary(event.data);
      return;
    }
    let message;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    handleMessage(message);
  };

  socket.onclose = (event) => {
    store.online = false;
    emit("connection", { online: false, code: event.code });
    if (event.code === 4401) {
      // Session vanished (server restart or kicked while away): reload so the
      // server re-renders the page with has_session=false → join overlay.
      location.reload();
      return;
    }
    scheduleReconnect();
  };

  socket.onerror = () => {
    try {
      socket.close();
    } catch { /* noop */ }
  };
}

/* Client-side watchdog: the server pings every 20 s; if nothing arrives
 * for 45 s the link is considered dead and we reconnect proactively. */
let lastInbound = Date.now();
setInterval(() => {
  if (store.stopped || !store.online) return;
  if (Date.now() - lastInbound > 45000) {
    console.warn("heartbeat lost, reconnecting");
    try {
      socket.close();
    } catch { /* noop */ }
  }
}, 5000);
