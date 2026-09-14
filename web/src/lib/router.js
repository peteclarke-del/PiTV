// Tiny hash router: "#/guide?day=2026-09-14" -> { path: '/guide', parts: ['guide'], query: {day: ...} }
import { route } from './stores.svelte.js';

function parse() {
  const hash = location.hash || '#/';
  const [pathPart, queryPart = ''] = hash.slice(1).split('?');
  const path = pathPart.startsWith('/') ? pathPart : `/${pathPart}`;
  const parts = path.split('/').filter(Boolean);
  const query = {};
  for (const [k, v] of new URLSearchParams(queryPart)) query[k] = v;
  return { path, parts, query, hash };
}

export function startRouter() {
  // A plain path such as /admin/acquire (bookmark or typed URL) is served by the SPA fallback; turn it into a hash route.
  if (!location.hash && location.pathname !== '/' && location.pathname !== '/index.html') {
    history.replaceState(null, '', `/#${location.pathname}${location.search}`);
  }
  const apply = () => {
    const r = parse();
    route.path = r.path;
    route.parts = r.parts;
    route.query = r.query;
    route.hash = r.hash;
  };
  apply();
  window.addEventListener('hashchange', apply);
}

export function navigate(path, query) {
  let hash = `#${path.startsWith('/') ? path : `/${path}`}`;
  if (query) {
    const qs = new URLSearchParams();
    for (const [k, v] of Object.entries(query)) if (v !== undefined && v !== null && v !== '') qs.set(k, v);
    const s = qs.toString();
    if (s) hash += `?${s}`;
  }
  if (location.hash !== hash) location.hash = hash;
}

/** Replace the query part of the current hash without adding a history entry. */
export function setQuery(query) {
  const r = parse();
  const merged = { ...r.query, ...query };
  const qs = new URLSearchParams();
  for (const [k, v] of Object.entries(merged)) if (v !== undefined && v !== null && v !== '') qs.set(k, v);
  const s = qs.toString();
  history.replaceState(null, '', `#${r.path}${s ? `?${s}` : ''}`);
  window.dispatchEvent(new HashChangeEvent('hashchange'));
}
