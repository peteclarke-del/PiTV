// Admin actions used from more than one page, each with its own toast.
import { post, tryApi } from './api.js';
import { toast } from './stores.svelte.js';

export const scanAll = () => tryApi(post('/api/scan'), { success: 'Scan started' });

export const buildSchedule = (force = false) =>
  tryApi(post('/api/schedule/build', force ? { force: true } : {}), { success: force ? 'Rebuild started' : 'Schedule build started' });

/** Verify tomorrow's files (substituting anything missing) and toast the verdict. Returns the report or undefined. */
export async function checkReadiness() {
  const r = await tryApi(post('/api/content/readiness', { days: 1, substitute: true }));
  if (r) (r.status === 'ok' ? toast.success : toast.error)(`Readiness ${r.status}: ${r.summary}`);
  return r;
}
