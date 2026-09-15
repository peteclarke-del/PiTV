<script>
  // Page controls for DataTable: position, page buttons and page size. `size` 0 shows every row.
  let { page = $bindable(0), size = $bindable(25), total = 0, sizes = [10, 25, 50, 100] } = $props();

  let pages = $derived(size ? Math.max(1, Math.ceil(total / size)) : 1);
  let first = $derived(total ? page * (size || total) + 1 : 0);
  let last = $derived(size ? Math.min(total, (page + 1) * size) : total);
  // At most seven buttons: the ends, the current page and its neighbours, gaps as ellipses.
  let buttons = $derived.by(() => {
    const want = new Set([0, pages - 1, page - 1, page, page + 1].filter((p) => p >= 0 && p < pages));
    const out = [];
    [...want].sort((a, b) => a - b).forEach((p, i, all) => {
      if (i && p - all[i - 1] > 1) out.push(null);
      out.push(p);
    });
    return out;
  });
  $effect(() => { if (page >= pages) page = pages - 1; });
</script>

<div class="pager small">
  <span class="muted">{first}–{last} of {total}</span>
  {#if pages > 1}
    <span class="btn-group">
      <button class="small ghost" disabled={page === 0} onclick={() => page--} aria-label="Previous page">‹</button>
      {#each buttons as p, i (p ?? `gap${i}`)}
        {#if p === null}<span class="gap">…</span>
        {:else}<button class="small ghost" class:active={p === page} aria-current={p === page ? 'page' : undefined} onclick={() => (page = p)}>{p + 1}</button>{/if}
      {/each}
      <button class="small ghost" disabled={page >= pages - 1} onclick={() => page++} aria-label="Next page">›</button>
    </span>
  {/if}
  <label class="size">Rows
    <select bind:value={size} onchange={() => (page = 0)} aria-label="Rows per page">
      {#each sizes as s (s)}<option value={s}>{s}</option>{/each}
      <option value={0}>All</option>
    </select>
  </label>
</div>

<style>
  .pager { display: flex; flex-wrap: wrap; align-items: center; gap: .5rem .9rem; padding: .5rem .75rem; }
  .pager .active { font-weight: 750; border-color: var(--border); }
  .gap { padding: 0 .3rem; color: var(--fg-muted); }
  .size { display: inline-flex; align-items: center; gap: .35rem; margin-left: auto; }
  .size select { width: auto; padding: .15rem .4rem; }
</style>
