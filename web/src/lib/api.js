// Fetch wrapper for the PiTV JSON API plus the single Server-Sent Events connection.
import { auth, player, jobs, changes, toast } from './stores.svelte.js';

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.status = status;
    this.detail = detail;
  }
}

function buildQuery(query) {
  if (!query) return '';
  const params = new URLSearchParams();
  for (const [k, v] of Object.entries(query)) {
    if (v === undefined || v === null || v === '') continue;
    params.set(k, String(v));
  }
  const s = params.toString();
  return s ? `?${s}` : '';
}

/** api('/api/now'), api('/api/player/key', {method:'POST', body:{key:'ok'}}), api('/api/media', {query:{kind:'movie'}}) */
export async function api(path, { method = 'GET', body, query } = {}) {
  const init = { method, headers: {}, credentials: 'same-origin' };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path + buildQuery(query), init);
  } catch (err) {
    throw new ApiError(0, 'Network error: is the PiTV service running?');
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!res.ok) {
    if (res.status === 401) {
      auth.admin = false;
      auth.needLogin = true;
    }
    const detail = data && typeof data === 'object' && 'detail' in data
      ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
      : (typeof data === 'string' && data) || res.statusText;
    throw new ApiError(res.status, detail);
  }
  return data;
}

export const get = (path, query) => api(path, { query });
export const post = (path, body = {}) => api(path, { method: 'POST', body });
export const put = (path, body = {}) => api(path, { method: 'PUT', body });
export const del = (path) => api(path, { method: 'DELETE' });

/** Run an API call, toasting the error if it fails. Returns undefined on failure. */
export async function tryApi(promise, { success } = {}) {
  try {
    const r = await promise;
    if (success) toast.success(success);
    return r;
  } catch (err) {
    toast.error(err.detail || err.message || String(err));
    return undefined;
  }
}

/** Normalise the player daemon's state (SSE `player` event, GET /api/player) for the UI.
 *  Online: {online:true, ts, channel{id,number,name,colour}|null, slot{id,kind,title,subtitle,start_ts,end_ts,media_id}|null,
 *  position, paused, behind_live, volume, muted, guide_open, playing, testcard, hwdec, on_pi, last_key, error,
 *  cache, maintenance, acquire, input_devices}. Offline: {online:false} or {ok:false, offline:true}. */
export function normalisePlayer(raw) {
  if (!raw || typeof raw !== 'object') return { online: false };
  const online = raw.online === true || raw.ok === true;
  if (!online) return { online: false, error: raw.error ?? null };
  const slot = raw.slot && typeof raw.slot === 'object' ? raw.slot : null;
  return {
    ...raw,
    online: true,
    channel: raw.channel && typeof raw.channel === 'object' ? raw.channel : null,
    slot,
    title: slot?.title ?? null,
    position: typeof raw.position === 'number' ? raw.position : null,
    paused: !!raw.paused,
    behind_live: !!raw.behind_live,
    muted: !!raw.muted,
    volume: typeof raw.volume === 'number' ? raw.volume : null,
    hwdec: raw.hwdec || null,
    cache: raw.cache ?? { enabled: false },
    maintenance: raw.maintenance ?? {},
    acquire: raw.acquire ?? { current: null },
    input_devices: Array.isArray(raw.input_devices) ? raw.input_devices : [],
    last_key: raw.last_key && typeof raw.last_key === 'object' ? raw.last_key : null,
  };
}

let es = null;
let hadHello = false;
let helloAt = 0;

/** Open the one EventSource used by the whole app. Idempotent. */
export function connectEvents() {
  if (es) return;
  es = new EventSource('/api/events');
  es.addEventListener('hello', (e) => {
    const data = safeJson(e.data);
    helloAt = Date.now();
    if (data?.player) player.state = normalisePlayer(data.player);
    if (hadHello) {
      // Reconnected after a drop: everything may be stale.
      changes.schedule++;
      changes.library++;
      changes.player++;
    }
    hadHello = true;
    player.connected = true;
  });
  es.addEventListener('player', (e) => {
    player.state = normalisePlayer(safeJson(e.data));
    changes.player++;
  });
  es.addEventListener('job', (e) => {
    const job = safeJson(e.data);
    if (!job || typeof job.id !== 'number') return;
    const i = jobs.list.findIndex((j) => j.id === job.id);
    if (i >= 0) jobs.list[i] = job; else jobs.list.push(job);
    if (jobs.list.length > 30) jobs.list.splice(0, jobs.list.length - 30);
    if (job.status === 'done' || job.status === 'failed') {
      if (job.kind === 'schedule') changes.schedule++;
      if (job.kind === 'scan') changes.library++;
    }
  });
  es.addEventListener('schedule', () => {
    if (Date.now() - helloAt < 500) return; // replayed "last event" on connect; the pages fetch anyway
    changes.schedule++;
  });
  es.addEventListener('library', () => {
    if (Date.now() - helloAt < 500) return;
    changes.library++;
  });
  es.onerror = () => { player.connected = false; };
}

function safeJson(s) {
  try { return JSON.parse(s); } catch { return null; }
}
