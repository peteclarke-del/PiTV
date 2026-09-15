// pitv_content's own local API, proxied by PiTV at /api/content/tool/api/{path}.
// Any failure (503 offline, connection error, or a foreign service answering on the port) counts as "offline".
import { api, ApiError } from './api.js';

const BASE = '/api/content/tool/api';

export const toolGet = (path, query) => api(`${BASE}/${path}`, { query });
export const toolPut = (path, body) => api(`${BASE}/${path}`, { method: 'PUT', body });
export const toolPost = (path, body = {}) => api(`${BASE}/${path}`, { method: 'POST', body });

/** True when the error means the tool API is not reachable (as opposed to a real 4xx from the tool). */
export function isOffline(err) {
  if (!(err instanceof ApiError)) return true;
  if (err.status === 0 || err.status === 503 || err.status === 502 || err.status === 504) return true;
  // The proxy forwards whatever answered on the port; an HTML error page means it wasn't pitv_content.
  if (err.status === 404 && /No route|<html|<!DOCTYPE/i.test(err.detail || '')) return true;
  return false;
}

// Background refreshes (Content status every 5 s, System host details every 10 s) would each log a
// 503 on the browser console while the API is down, so once a probe finds it offline the next one
// waits PROBE_MS. The wait is shared by every page: there is only one pitv_content.
const PROBE_MS = 30000;
let offlineAt = 0;

/** toolGet for background refreshes. Resolves undefined while the API is known to be offline and the
 *  wait is not over. A document failing `looksRight` (another service answering on the port) counts
 *  as offline. */
export async function toolProbe(path, looksRight = () => true) {
  if (offlineAt && Date.now() - offlineAt < PROBE_MS) return undefined;
  try {
    const doc = await toolGet(path);
    if (!doc || typeof doc !== 'object' || !looksRight(doc)) throw new ApiError(0, 'not pitv_content');
    offlineAt = 0;
    return doc;
  } catch (e) {
    if (isOffline(e)) offlineAt = Date.now();
    throw e;
  }
}

/** Field errors from a 400 {"errors": {key: message}} response, else null. */
export function fieldErrors(err) {
  if (!(err instanceof ApiError) || err.status !== 400) return null;
  const d = err.body;
  return d && typeof d === 'object' && d.errors && typeof d.errors === 'object' ? d.errors : null;
}
