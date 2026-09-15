// Small browser helpers with no PiTV knowledge.

/** Collapse rapid calls into one, run `ms` after the last call. */
export function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}

/** Offer `data` as a pretty-printed JSON download. */
export function downloadJson(data, filename) {
  const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

/** Coerce a form value to a number within [min, max] before it goes on the wire.
 *  Blank or non-numeric input gives `fallback` (null unless given), so a field can be cleared. */
export function num(v, { min = -Infinity, max = Infinity, int = false, fallback = null } = {}) {
  if (v === '' || v === null || v === undefined) return fallback;
  let n = Number(v);
  if (!Number.isFinite(n)) return fallback;
  if (int) n = Math.trunc(n);
  return Math.min(max, Math.max(min, n));
}

/** "Comedy, Drama," -> ['Comedy', 'Drama'] */
export const splitList = (s) => String(s ?? '').split(',').map((t) => t.trim()).filter(Boolean);

/** Move arr[i] by `d` places in place (works on $state proxies); moves past either end are ignored. */
export function moveItem(arr, i, d) {
  const j = i + d;
  if (j < 0 || j >= arr.length) return;
  arr.splice(j, 0, ...arr.splice(i, 1));
}

/** Keyboard twin of an onclick handler for focusable non-button elements (table rows). */
export const onEnter = (fn) => (e) => { if (e.key === 'Enter') fn(e); };
