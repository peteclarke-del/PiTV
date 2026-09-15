// App-wide reactive state (Svelte 5 runes). Import and read/mutate properties directly.

export const route = $state({ path: '/', parts: [], query: {} });

export const auth = $state({ checked: false, password_set: false, admin: true });

export const player = $state({ state: { online: false }, connected: false });

export const jobs = $state({ list: [] });

/** Counters bumped by SSE events; pages watch them with $effect to refetch. */
export const changes = $state({ schedule: 0, library: 0 });

/** Wall clock in seconds, ticked once a second by App. */
export const clock = $state({ ts: Math.floor(Date.now() / 1000) });

export const toasts = $state({ list: [] });
let toastId = 1;
function pushToast(kind, text, ms) {
  const id = toastId++;
  toasts.list.push({ id, kind, text });
  setTimeout(() => dismissToast(id), ms);
  return id;
}
function dismissToast(id) {
  const i = toasts.list.findIndex((t) => t.id === id);
  if (i >= 0) toasts.list.splice(i, 1);
}
export const toast = {
  success: (text) => pushToast('success', text, 3500),
  error: (text) => pushToast('error', text, 7000),
  info: (text) => pushToast('info', text, 4000),
  dismiss: dismissToast,
};

/** Promise-based confirm dialog rendered by App. */
export const confirmState = $state({ open: false, title: '', message: '', okLabel: 'OK', danger: false, resolve: null });
export function confirm(message, { title = 'Are you sure?', okLabel = 'Confirm', danger = false } = {}) {
  return new Promise((resolve) => {
    Object.assign(confirmState, { title, message, okLabel, danger, resolve, open: true });
  });
}
export function answerConfirm(value) {
  const r = confirmState.resolve;
  confirmState.open = false;
  confirmState.resolve = null;
  if (r) r(value);
}
