// Helpers shared by the guide and the schedule editor for GET /api/schedule/days
// ({today, days: [{day, n, s, e}], horizon_end}).

/** Unix bounds for drawing one broadcast day: its first slot up to the next day's first slot
 *  (or 8 h past its last slot, so the overnight replay of the final day is visible). */
export function dayBounds(days, day) {
  if (!days) return null;
  const i = days.days.findIndex((d) => d.day === day);
  if (i < 0) return null;
  const d = days.days[i], next = days.days[i + 1];
  return { start: d.s, end: next ? next.s : d.e + 8 * 3600 };
}

export const hasDay = (days, day) => !!days?.days.some((d) => d.day === day);

/** Keep `day` if it is still built, else today, else the first built day. */
export function pickDay(days, day) {
  if (hasDay(days, day)) return day;
  return hasDay(days, days.today) ? days.today : (days.days[0]?.day ?? '');
}

/** Today's built day, or null when the schedule only starts tomorrow (so "Now" must not switch to it). */
export const builtToday = (days) => (hasDay(days, days?.today) ? days.today : null);

/** EPG pixels per minute: a little tighter on phones. */
export const defaultPpm = () => (window.matchMedia('(max-width: 600px)').matches ? 3 : 4);
