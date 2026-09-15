<script>
  // Every table of records in the admin: filter, sort by any column (ascending, descending,
  // off), drag a header (or Alt+arrow on it) to move its column, and page through the rows.
  // Sort, column order and page size are remembered per table (`id`) in this browser.
  //
  // columns: [{ key, label, title? (header tooltip), get?(row) -> value to sort and search on
  //             (default row[key]), cell?: snippet(row, column) to render (default the value),
  //             class?, sortable? (default true) }]
  // A cell with the class `control` (a switch, a button) keeps full opacity on a dimmed row.
  import { untrack } from 'svelte';
  import Pager from './Pager.svelte';
  import { tablePrefs } from '../lib/prefs.svelte.js';
  import { filterRows, moved, nextSort, ordered, rowText, sortRows, valueOf } from '../lib/table.js';

  let {
    id, columns, rows = null, key = (r) => r.id, search = false, sort: defaultSort = null, size: defaultSize = 25,
    onrow = null, rowClass = null, empty = 'Nothing to show.', toolbar = null, card = true,
  } = $props();

  // Created once: a table keeps its id for its lifetime.
  const saved = untrack(() => tablePrefs(id, { sort: defaultSort, size: defaultSize }));
  let text = $state('');
  let page = $state(0);
  let dragFrom = $state(null);
  let dragOver = $state(null);

  let cols = $derived(ordered(columns, saved.order));
  let matching = $derived(rows ? filterRows(rows, text, (r) => rowText(columns, r)) : []);
  let sorted = $derived(sortRows(matching, columns, saved.sort));
  let shownRows = $derived(saved.size ? sorted.slice(page * saved.size, (page + 1) * saved.size) : sorted);

  $effect(() => { text; saved.sort; page = 0; });

  function toggle(k) { saved.sort = nextSort(saved.sort, k); }
  function move(from, to) {
    if (from === to || to < 0 || to >= cols.length) return;
    saved.order = moved(cols.map((c) => c.key), from, to);
  }
  function headerKey(e, i) {
    if (!e.altKey || (e.key !== 'ArrowLeft' && e.key !== 'ArrowRight')) return;
    e.preventDefault();
    const to = i + (e.key === 'ArrowLeft' ? -1 : 1);
    const row = e.currentTarget.closest('tr');   // currentTarget is gone once the event returns
    move(i, to);
    // Keep focus on the moved header so repeated presses keep moving it.
    requestAnimationFrame(() => row?.children[to]?.querySelector('button')?.focus());
  }
  const indicator = (k) => (saved.sort?.key !== k ? '' : saved.sort.dir === 'asc' ? '▲' : '▼');
  const ariaSort = (k) => (saved.sort?.key !== k ? 'none' : saved.sort.dir === 'asc' ? 'ascending' : 'descending');
  const display = (c, r) => { const v = valueOf(c, r); return v === null || v === undefined || v === '' ? '–' : v; };
  const activate = (r) => (e) => { if (e.key === 'Enter' && e.target === e.currentTarget) onrow(r); };
</script>

<div class="dt" class:card class:pad-0={card}>
  {#if search || toolbar}
    <div class="dt-bar">
      {#if search}<input type="search" placeholder={typeof search === 'string' ? search : 'Filter…'} aria-label="Filter rows" bind:value={text} />{/if}
      {@render toolbar?.()}
    </div>
  {/if}
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          {#each cols as c, i (c.key)}
            <th class={c.class} aria-sort={ariaSort(c.key)} draggable="true" class:dragging={dragFrom === i} class:over={dragOver === i && dragFrom !== i}
              ondragstart={(e) => { dragFrom = i; e.dataTransfer.effectAllowed = 'move'; e.dataTransfer.setData('text/plain', c.key); }}
              ondragover={(e) => { e.preventDefault(); dragOver = i; }}
              ondragleave={() => { if (dragOver === i) dragOver = null; }}
              ondrop={(e) => { e.preventDefault(); if (dragFrom !== null) move(dragFrom, i); dragFrom = dragOver = null; }}
              ondragend={() => (dragFrom = dragOver = null)}>
              {#if c.sortable !== false}
                <button type="button" class="th-sort" onclick={() => toggle(c.key)} onkeydown={(e) => headerKey(e, i)}
                  title="{c.title ? `${c.title}. ` : ''}Sort by {c.title ?? c.label}. Drag, or Alt+arrow, to move the column.">{c.label}{#if indicator(c.key)}<span class="ind" aria-hidden="true">{indicator(c.key)}</span>{/if}</button>
              {:else}<span class="th-plain" title={c.title}>{c.label}</span>{/if}
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each shownRows as r (key(r))}
          <tr class={rowClass?.(r) ?? ''} class:clickable={!!onrow} tabindex={onrow ? 0 : undefined}
            onclick={onrow ? () => onrow(r) : undefined} onkeydown={onrow ? activate(r) : undefined}>
            {#each cols as c (c.key)}
              <td class={c.class}>{#if c.cell}{@render c.cell(r, c)}{:else}{display(c, r)}{/if}</td>
            {/each}
          </tr>
        {:else}
          <tr><td colspan={cols.length} class="empty">{rows ? (text ? 'No rows match the filter.' : empty) : 'Loading…'}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  {#if rows?.length}<Pager bind:page bind:size={saved.size} total={sorted.length} />{/if}
</div>

<style>
  .dt-bar { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem; padding: .6rem .75rem 0; }
  .dt-bar input[type="search"] { flex: 1; max-width: 360px; }
  th { user-select: none; vertical-align: middle; }
  th[draggable="true"] { cursor: grab; }
  th.dragging { opacity: .45; }
  th.over { box-shadow: inset 2px 0 0 var(--info); }
  .th-sort { all: unset; cursor: pointer; display: inline-flex; align-items: center; gap: .3rem; font: inherit; color: inherit; text-transform: inherit; letter-spacing: inherit; }
  .th-sort:focus-visible { outline: 2px solid var(--info); outline-offset: 2px; border-radius: 2px; }
  .ind { font-size: .65rem; color: var(--info); }
  :global(th.num) .th-sort { flex-direction: row-reverse; }
</style>
