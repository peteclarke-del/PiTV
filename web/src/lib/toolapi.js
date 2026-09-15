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

/** Field errors from a 400 {"errors": {key: message}} response, else null. */
export function fieldErrors(err) {
  if (!(err instanceof ApiError) || err.status !== 400) return null;
  const d = err.body;
  return d && typeof d === 'object' && d.errors && typeof d.errors === 'object' ? d.errors : null;
}
