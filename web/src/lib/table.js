// Sorting, filtering, paging and column order for DataTable. Pure functions over plain rows.

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: 'base' });
const empty = (v) => v === null || v === undefined || v === '';

/** Order two cell values: blanks last, numbers numerically, everything else as text. */
export function compare(a, b) {
  if (empty(a) || empty(b)) return empty(a) - empty(b);
  if (typeof a === 'number' && typeof b === 'number') return a - b;
  if (typeof a === 'boolean' || typeof b === 'boolean') return Number(a) - Number(b);
  return collator.compare(String(a), String(b));
}

/** A column's sortable, searchable value for a row. */
export const valueOf = (col, row) => (col.get ? col.get(row) : row?.[col.key]);

/** Rows sorted by `sort` ({key, dir}); stable, blanks last in either direction. */
export function sortRows(rows, columns, sort) {
  const col = sort && columns.find((c) => c.key === sort.key);
  if (!col) return rows;
  const sign = sort.dir === 'desc' ? -1 : 1;
  return rows
    .map((row, i) => ({ row, i, v: valueOf(col, row) }))
    .sort((x, y) => (empty(x.v) || empty(y.v) ? compare(x.v, y.v) : sign * compare(x.v, y.v)) || x.i - y.i)
    .map((x) => x.row);
}

/** Rows whose searchable text contains every word of `text`, case-insensitively. */
export function filterRows(rows, text, toText) {
  const words = String(text ?? '').toLowerCase().split(/\s+/).filter(Boolean);
  if (!words.length) return rows;
  return rows.filter((row) => {
    const hay = toText(row).toLowerCase();
    return words.every((w) => hay.includes(w));
  });
}

/** The searchable text of a row: every column's value that is text or a number. */
export function rowText(columns, row) {
  return columns.map((c) => valueOf(c, row)).filter((v) => typeof v === 'string' || typeof v === 'number').join(' ');
}

/** Columns in the saved order; columns the saved order does not know keep their place at the end. */
export function ordered(columns, order) {
  const at = new Map((order ?? []).map((k, i) => [k, i]));
  return [...columns].sort((a, b) => (at.get(a.key) ?? Infinity) - (at.get(b.key) ?? Infinity));
}

/** `keys` with the key at `from` moved to index `to`. */
export function moved(keys, from, to) {
  const out = [...keys];
  const [k] = out.splice(from, 1);
  out.splice(Math.max(0, Math.min(to, out.length)), 0, k);
  return out;
}

/** The next sort after a click on `key`: ascending, then descending, then none. */
export function nextSort(sort, key) {
  if (sort?.key !== key) return { key, dir: 'asc' };
  return sort.dir === 'asc' ? { key, dir: 'desc' } : null;
}
