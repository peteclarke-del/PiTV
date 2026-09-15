// How each settings field type is cleaned before it is saved. Ranges and choices come from the
// field itself (the server's settings schema), so no bound is written twice.
import { num } from './util.js';

const bounded = (v, min, max, int, fallback) => num(v, { min: min ?? -Infinity, max: max ?? Infinity, int, fallback });
const weight = (v) => bounded(v, 0, Infinity, false, 0);

function daypart(d) {
  const out = { name: d.name ?? '', start: d.start, tv: weight(d.tv), movie: weight(d.movie), kids: weight(d.kids), sport: weight(d.sport) };
  const max = num(d.max_minutes, { min: 1, int: true });
  if (max !== null) out.max_minutes = max;
  return out;
}

function musicBlock(b) {
  const out = { start: b.start, name: b.name ?? '', genres: (b.genres ?? []).map((g) => String(g).toLowerCase()),
                decades: [...new Set((b.decades ?? []).map(Number))].sort((x, y) => x - y) };
  if (b.concert) out.concert = true;
  return out;
}

const CLEAN = {
  int: (f, v) => bounded(v, f.min, f.max, true, f.default),
  float: (f, v) => bounded(v, f.min, f.max, false, f.default),
  slider: (f, v) => bounded(v, f.min, f.max, false, f.default),
  chips: (f, v) => (v ?? []).map((x) => String(x).trim()).filter(Boolean).map((x) => (f.options?.lower ? x.toLowerCase() : x)),
  hour_list: (f, v) => [...new Set((v ?? []).map((h) => num(h, { min: 0, max: 23, int: true })).filter((h) => h !== null))].sort((a, b) => a - b),
  decades: (f, v) => [...new Set((v ?? []).map(Number))].sort((a, b) => a - b),
  kind_weights: (f, v) => ({ tv: bounded(v?.tv, 0, 1, false, 0), movie: bounded(v?.movie, 0, 1, false, 0) }),
  dayparts: (f, v) => (v ?? []).map(daypart),
  music_blocks: (f, v) => (v ?? []).map(musicBlock),
  hours: (f, v) => String(v ?? '').trim(),
};

/** The value to send for field `f`, cleaned for its type. */
export const clean = (f, v) => (CLEAN[f.type] ? CLEAN[f.type](f, v) : v);

/** A blank value of the field's type, for fields the server sent without a value. */
export function blank(type) {
  if (type === 'bool') return false;
  if (['list', 'chips', 'hour_list', 'decades', 'dayparts', 'music_blocks'].includes(type)) return [];
  if (['weights', 'times', 'kind_weights'].includes(type)) return {};
  return type === 'int' || type === 'float' || type === 'slider' ? 0 : '';
}
