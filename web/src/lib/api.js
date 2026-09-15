// Fetch wrapper for the PiTV JSON API, the shared "confirm → call → toast" helpers, and the
// single Server-Sent Events connection that feeds the player/jobs/changes stores.
import { auth, player, jobs, changes, toast, confirm } from './stores.svelte.js';

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
  } catch {
    throw new ApiError(0, 'Network error: is the PiTV service running?');
  }
  let data = null;
  const text = await res.text();
  if (text) {
    try { data = JSON.parse(text); } catch { data = text; }
  }
  if (!res.ok) {
    if (res.status === 401) auth.admin = false; // Admin page shows the login form
    const detail = data && typeof data === 'object' && 'detail' in data
      ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail))
      : data && typeof data === 'object' && typeof data.error === 'string' ? data.error
      : (typeof data === 'string' && data) || res.statusText;
    const err = new ApiError(res.status, detail);
    err.body = data;
    throw err;
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

/** Ask the user first, then run `call()` through tryApi. Returns undefined when cancelled or failed. */
export async function confirmApi(message, opts, call, { success } = {}) {
  if (!(await confirm(message, opts))) return undefined;
  return tryApi(call(), { success });
}

/** Refresh the auth store from GET /api/auth (admin is implied while no password is set). */
export async function refreshAuth() {
  try {
    const a = await get('/api/auth');
    auth.password_set = a.password_set;
    auth.admin = a.admin;
  } catch { /* keep the previous answer */ }
  auth.checked = true;
}

/** Normalise the player daemon's state (SSE `player` event, GET /api/player, /api/now) for the UI.
 *  Online: {online:true, ts, channel{id,number,name,colour}|null, slot{id,kind,title,subtitle,start_ts,end_ts,media_id}|null,
 *  position, paused, behind_live, volume, muted, guide_open, playing, testcard, hwdec, on_pi, last_key, error,
 *  cache, maintenance, stream, file, input_devices}. Offline: {online:false, error}. */
export function normalisePlayer(raw) {
  if (!raw || typeof raw !== 'object' || raw.online !== true) return { online: false, error: raw?.error ?? null };
  return {
    ...raw,
    channel: raw.channel && typeof raw.channel === 'object' ? raw.channel : null,
    slot: raw.slot && typeof raw.slot === 'object' ? raw.slot : null,
    position: typeof raw.position === 'number' ? raw.position : null,
    paused: !!raw.paused,
    behind_live: !!raw.behind_live,
    muted: !!raw.muted,
    volume: typeof raw.volume === 'number' ? raw.volume : null,
    hwdec: raw.hwdec || null,
    cache: raw.cache ?? { enabled: false },
    maintenance: raw.maintenance ?? {},
    input_devices: Array.isArray(raw.input_devices) ? raw.input_devices : [],
    last_key: raw.last_key && typeof raw.last_key === 'object' ? raw.last_key : null,
  };
}

// --- Server-Sent Events -------------------------------------------------------------------------
// One EventSource for the whole app. The server replays its last event of each kind right after
// `hello`, so schedule/library events within 500 ms of a (re)connect are ignored: the pages fetch
// on mount anyway, and on a reconnect the change counters are bumped explicitly instead.

let es = null;
let hadHello = false;
let helloAt = 0;

/** Open the shared EventSource. Idempotent. */
export function connectEvents() {
  if (es) return;
  es = new EventSource('/api/events');
  es.addEventListener('hello', (e) => {
    const data = safeJson(e.data);
    helloAt = Date.now();
    if (data?.player) player.state = normalisePlayer(data.player);
    if (hadHello) { // reconnected after a drop: everything may be stale
      changes.schedule++;
      changes.library++;
    }
    hadHello = true;
    player.connected = true;
  });
  es.addEventListener('player', (e) => { player.state = normalisePlayer(safeJson(e.data)); });
  es.addEventListener('job', (e) => {
    const job = safeJson(e.data);
    if (!job || typeof job.id !== 'number') return;
    upsertJob(job);
    if (jobs.list.length > 30) jobs.list.splice(0, jobs.list.length - 30);
    if (job.status === 'done' || job.status === 'failed') {
      if (job.kind === 'schedule') changes.schedule++;
      if (job.kind === 'scan') changes.library++;
    }
  });
  es.addEventListener('schedule', () => { if (Date.now() - helloAt >= 500) changes.schedule++; });
  es.addEventListener('library', () => { if (Date.now() - helloAt >= 500) changes.library++; });
  es.onerror = () => { player.connected = false; };
}

/** Merge a job record (from SSE or GET /api/jobs) into the jobs store. */
export function upsertJob(job) {
  const i = jobs.list.findIndex((j) => j.id === job.id);
  if (i >= 0) jobs.list[i] = job; else jobs.list.push(job);
}

function safeJson(s) {
  try { return JSON.parse(s); } catch { return null; }
}
