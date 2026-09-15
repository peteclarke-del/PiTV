// Admin actions used from more than one page, each with its own toast.
import { post, tryApi } from './api.js';
import { toast } from './stores.svelte.js';

/** Import pitv_content's library index into PiTV's catalogue; with reindex, ask pitv_content to re-scan its sources first. */
export const importCatalogue = (reindex = false) =>
  tryApi(post('/api/catalogue/refresh', { reindex }), { success: reindex ? 'Re-index requested; the import follows when pitv_content finishes' : 'Catalogue import started' });

export const buildSchedule = (force = false) =>
  tryApi(post('/api/schedule/build', force ? { force: true } : {}), { success: force ? 'Rebuild started' : 'Schedule build started' });

/** Verify tomorrow's files (substituting anything missing) and toast the verdict. Returns the report or undefined. */
export async function checkReadiness() {
  const r = await tryApi(post('/api/content/readiness', { days: 1, substitute: true }));
  if (r) (r.status === 'ok' ? toast.success : toast.error)(`Readiness ${r.status}: ${r.summary}`);
  return r;
}
