// Classify the player's error message (contract section 6): a NAS fallback is a warning, an unplayable
// programme puts the technical difficulties card on air.
export function playbackIssue(error) {
  const e = String(error ?? '');
  if (!e) return null;
  if (/not playable|technical difficulties/i.test(e)) return { cls: 'danger', label: 'Technical difficulties' };
  if (/\bnas\b/i.test(e)) return { cls: 'warn', label: 'NAS fallback' };
  return { cls: 'danger', label: 'error' };
}

/** What the subtitles switch is doing, for the remote and the status badges. Off and on are the
 *  viewer's choice; on with no track says why nothing is on screen and where the language is set. */
export function subtitleNote(state) {
  if (!state?.subtitles) return 'Subtitles are off';
  const t = state.subtitle_track;
  if (t) return `Showing the ${t.lang ?? 'unlabelled'} subtitle track${t.external ? ' from a file beside the programme' : ''}`;
  if (!state.playing) return 'Subtitles are on and will show when a programme has them';
  return 'Subtitles are on, but this programme has none in the chosen language. The language is set in Settings, Player.';
}
