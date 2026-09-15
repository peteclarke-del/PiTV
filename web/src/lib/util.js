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

/** Keyboard twin of an onclick handler for focusable non-button elements (table rows). */
export const onEnter = (fn) => (e) => { if (e.key === 'Enter') fn(e); };
