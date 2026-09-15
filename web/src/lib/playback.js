// Classify the player's error message (contract section 6): a NAS fallback is a warning, an unplayable
// programme puts the technical difficulties card on air.
export function playbackIssue(error) {
  const e = String(error ?? '');
  if (!e) return null;
  if (/not playable|technical difficulties/i.test(e)) return { cls: 'danger', label: 'Technical difficulties' };
  if (/\bnas\b/i.test(e)) return { cls: 'warn', label: 'NAS fallback' };
  return { cls: 'danger', label: 'error' };
}
