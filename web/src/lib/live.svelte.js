// Live connection to the game server. Every `state` message is a full snapshot,
// so a client that reconnects or wakes up is in sync immediately.
import { io } from 'socket.io-client';

function storage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

export function load(key, fallback = null) {
  try {
    const v = storage()?.getItem(key);
    return v === null || v === undefined ? fallback : v;
  } catch {
    return fallback;
  }
}

export function save(key, value) {
  try {
    if (value === null || value === undefined) storage()?.removeItem(key);
    else storage()?.setItem(key, value);
  } catch {}
}

function randomId() {
  if (window.crypto?.randomUUID) return window.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  (window.crypto || window.msCrypto).getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}

/** A random id per browser, kept in localStorage. No login, nothing personal. */
export function deviceId() {
  let id = load('minimax.device');
  if (!id) {
    id = randomId();
    save('minimax.device', id);
  }
  return id;
}

/**
 * @param {{role: 'phone'|'board'|'admin', token?: string, team?: () => string|null}} opts
 */
export function connect({ role, token, team }) {
  const live = $state({
    connected: false,
    everConnected: false,
    error: null,
    state: null,
    tree: null,
    search: null,
    status: null,
    hello: null,
    offset: 0,
  });

  const socket = io({
    auth: token ? { role, token } : { role },
    reconnectionDelay: 500,
    reconnectionDelayMax: 4000,
    timeout: 10000,
  });

  const device = deviceId();

  async function hello() {
    try {
      const ack = await socket.timeout(8000).emitWithAck('hello', { device_id: device, team: team?.() ?? null });
      if (ack?.server_time) live.offset = ack.server_time - Date.now();
      live.hello = ack;
    } catch {
      // the next reconnect or visibility change says hello again
    }
  }

  socket.on('connect', () => {
    live.connected = true;
    live.everConnected = true;
    live.error = null;
    hello();
  });
  socket.on('disconnect', () => {
    live.connected = false;
  });
  socket.on('connect_error', (err) => {
    live.connected = false;
    live.error = err?.message || 'cannot reach the server';
  });
  socket.on('state', (s) => {
    live.offset = s.server_time - Date.now();
    live.state = s;
  });
  socket.on('tree', (t) => {
    live.tree = t.tree;
    live.search = t.search || null;
  });
  socket.on('search', (p) => {
    live.search = p?.cleared ? null : p;
  });
  socket.on('status', (s) => {
    live.status = s;
  });

  // Phones sleep and wake up: reconnect or resync when the page becomes visible again.
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState !== 'visible') return;
    if (!socket.connected) socket.connect();
    else hello();
  });
  window.addEventListener('online', () => {
    if (!socket.connected) socket.connect();
  });

  /** Emit with acknowledgement; resolves to the server's reply or {ok:false, error}. */
  async function call(event, data = {}, timeoutMs = 8000) {
    try {
      return await socket.timeout(timeoutMs).emitWithAck(event, data);
    } catch {
      return { ok: false, error: 'no answer from the server' };
    }
  }

  return { live, socket, call, hello, device };
}

/** Server time in ms, using the offset measured on the last state message. */
export function serverNow(live) {
  return Date.now() + (live.offset || 0);
}
