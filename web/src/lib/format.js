// Formatting helpers and the small domain constants the admin forms share.
// Times are shown in the browser's local zone, 24h clock.

const timeFmt = new Intl.DateTimeFormat(undefined, { hour: '2-digit', minute: '2-digit', hour12: false });
const dayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short' });
const longDayFmt = new Intl.DateTimeFormat(undefined, { weekday: 'long', day: 'numeric', month: 'long' });
const dateTimeFmt = new Intl.DateTimeFormat(undefined, { weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', hour12: false });

// Some engines print midnight as "24:00" with hour12:false.
export function fmtTime(ts) {
  if (ts === null || ts === undefined) return '';
  return timeFmt.format(new Date(ts * 1000)).replace(/^24:/, '00:');
}

export function fmtRange(a, b) {
  return `${fmtTime(a)}–${fmtTime(b)}`;
}

export function fmtDateTime(ts) {
  if (!ts) return '';
  return dateTimeFmt.format(new Date(ts * 1000)).replace(/24:(\d\d)$/, '00:$1');
}

/** 'YYYY-MM-DD' -> 'Mon 14 Sep' */
export function fmtDay(day, long = false) {
  if (!day) return '';
  const [y, m, d] = day.split('-').map(Number);
  return (long ? longDayFmt : dayFmt).format(new Date(y, m - 1, d));
}

export function fmtDuration(seconds) {
  if (!seconds && seconds !== 0) return '';
  const m = Math.round(seconds / 60);
  if (m < 60) return `${m} min`;
  const h = Math.floor(m / 60);
  const r = m % 60;
  return r ? `${h}h ${r}m` : `${h}h`;
}

/** Uptime as its two largest units: "6 days 18 h", "18 h 39 min", "39 min". */
export function fmtUptime(seconds) {
  const d = Math.floor(seconds / 86400);
  const h = Math.floor((seconds % 86400) / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (d) return `${d} day${d === 1 ? '' : 's'} ${h} h`;
  return h ? `${h} h ${m} min` : `${m} min`;
}

export function fmtBytes(n) {
  if (n === null || n === undefined) return '';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) { v /= 1024; i++; }
  return `${v.toFixed(i >= 3 ? 1 : 0)} ${units[i]}`;
}

export function fmtAgo(ts, now = Math.floor(Date.now() / 1000)) {
  if (!ts) return 'never';
  const d = now - ts;
  if (d < 60) return 'just now';
  if (d < 3600) return `${Math.floor(d / 60)} min ago`;
  if (d < 86400) return `${Math.floor(d / 3600)} h ago`;
  return `${Math.floor(d / 86400)} d ago`;
}

/** (1, 3) -> 'S01E03'; missing numbers count as 0. */
export function fmtEpisode(season, episode) {
  return `S${String(season ?? 0).padStart(2, '0')}E${String(episode ?? 0).padStart(2, '0')}`;
}

/** Local 'YYYY-MM-DD' + 'HH:MM' -> unix seconds */
export function localToTs(day, time) {
  const [y, m, d] = day.split('-').map(Number);
  const [hh, mm] = time.split(':').map(Number);
  return Math.floor(new Date(y, m - 1, d, hh, mm, 0).getTime() / 1000);
}

export function tsToLocalDay(ts) {
  const d = new Date(ts * 1000);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
}

export function tsToLocalTime(ts) {
  const d = new Date(ts * 1000);
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/** Badge modifier class for a job / run / wanted status ('' = neutral, e.g. queued or idle). */
export function statusClass(status) {
  switch (status) {
    case 'ok': case 'done': return 'ok';
    case 'running': case 'searching': case 'downloading': case 'transcoding': return 'info';
    case 'warning': case 'cancelled': return 'warn';
    case 'error': case 'failed': return 'danger';
    default: return '';
  }
}

/** A channel colour as a CSS value. Anything but #rrggbb (the colour is admin-typed text that ends
 *  up inside a style attribute) falls back to grey rather than being interpolated into CSS. */
export function safeColour(hex) {
  return typeof hex === 'string' && /^#[0-9a-f]{6}$/i.test(hex) ? hex : '#888888';
}

export const WEEKDAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
export const CERTIFICATES = ['U', 'PG', '12', '12A', '15', '18'];
// What a programme is (pitv/genres.py PROGRAMME_TYPES): this, not its genres, decides its channel.
export const PROGRAMME_TYPES = [['film', 'Film'], ['series', 'TV series'], ['documentary', 'Documentary'],
                                ['cartoon', 'Cartoon'], ['music', 'Music'], ['sport', 'Sport']];
export const programmeTypeLabel = (t) => PROGRAMME_TYPES.find(([v]) => v === t)?.[1] ?? t;
export const PATTERN_TOKENS = ['show', 'tv', 'movie', 'ad', 'ident', 'break'];

/** Whether the backend says this channel's scheduling pattern draws from a line-up. */
export const hasLineup = (channel) => channel?.has_lineup === true;

export function plural(n, word) {
  return `${n} ${word}${n === 1 ? '' : 's'}`;
}

/** A line-up entry's progress as [badge class, text]: fetching, scheduled and waiting, on disk, or neither. */
export function lineupState(e) {
  if (e.wanted_open) return ['info', `fetching ${e.wanted_open}`];
  if (e.placeholders) return ['warn', `${e.placeholders} scheduled`];
  if (e.on_disk) return ['ok', e.kind === 'show' && e.episodes_on_disk ? `on disk (${e.episodes_on_disk} eps)` : 'on disk'];
  return ['', 'not on disk'];
}

/** `url` if it is an http(s) address (https only when `secure`), else null: links and images
 *  from outside sources must never become javascript: or data: URLs. */
export function safeUrl(url, secure = false) {
  try {
    const u = new URL(String(url ?? ''));
    return u.protocol === 'https:' || (!secure && u.protocol === 'http:') ? u.href : null;
  } catch {
    return null;
  }
}

/** A screen profile's quality in words: what pitv_content encodes to and the best source it fetches. */
export function fmtProfile(p) {
  if (!p) return '–';
  const fps = p.frame_rate ? `, ${Math.round(p.frame_rate * 100) / 100} fps` : '';
  return `${p.label}: encodes to ${p.width}×${p.height} ${String(p.vcodec).toUpperCase()} (${p.aspect}${fps}), `
    + `from the best source up to ${p.max_source_height >= 2160 ? '4K' : `${p.max_source_height}p`}`;
}
