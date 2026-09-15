// Per-browser admin preferences: the familiarity level, and each table's sort, column order and
// page size. They are conveniences, so they live in localStorage; when storage is unavailable
// (a private window, blocked site data) they last until the page is reloaded.

const KEY = 'pitv-admin-prefs';

/** Familiarity levels, least to most: each shows everything the ones before it show. */
export const LEVELS = [
  ['basic', 'Basic', 'What a household needs to run the set'],
  ['standard', 'Standard', 'Shaping the schedule and the catalogue'],
  ['advanced', 'Advanced', "The scheduler's arithmetic, the player's plumbing and pitv_content's internals"],
];
const RANK = Object.fromEntries(LEVELS.map(([id], i) => [id, i]));

function read() {
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? '{}');
    return {
      level: RANK[saved?.level] !== undefined ? saved.level : 'standard',
      tables: saved?.tables && typeof saved.tables === 'object' ? saved.tables : {},
    };
  } catch {
    return { level: 'standard', tables: {} };
  }
}

export const prefs = $state(read());

$effect.root(() => {
  $effect(() => {
    const doc = JSON.stringify(prefs);
    try { localStorage.setItem(KEY, doc); } catch { /* see the header */ }
  });
});

/** Whether something marked `level` shows at the chosen familiarity level (unmarked: standard). */
export const shown = (level) => (RANK[level ?? 'standard'] ?? 1) <= RANK[prefs.level];

/** One table's saved state, created on first use: {sort: {key, dir} | null, order: [keys], size}. */
export function tablePrefs(id, defaults) {
  prefs.tables[id] ??= { sort: defaults.sort ?? null, order: [], size: defaults.size ?? 25 };
  return prefs.tables[id];
}
