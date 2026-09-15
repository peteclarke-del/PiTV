<script>
  // Compact multi-select: a button ("Any genre" / "3 selected") opening a popover with a filter and checkboxes.
  let { value = [], options = {}, onchange, empty = 'Any genre', label = 'Genres' } = $props();
  let open = $state(false);
  let filter = $state('');
  let root = $state(null);
  let names = $derived(Object.keys(options).sort((a, b) => a.localeCompare(b)));
  let shown = $derived(names.filter((n) => !filter || n.toLowerCase().includes(filter.toLowerCase())));
  let selected = $derived(new Set((value ?? []).map((v) => String(v).toLowerCase())));
  function toggle(name) {
    const key = name.toLowerCase();
    const next = (value ?? []).filter((v) => String(v).toLowerCase() !== key);
    if (!selected.has(key)) next.push(name);
    onchange?.(next);
  }
  function onDoc(e) { if (open && root && !root.contains(e.target)) open = false; }
  // Escape closes the popover only; stopping it here keeps an enclosing drawer open.
  function onKey(e) { if (e.key === 'Escape' && open) { open = false; e.stopPropagation(); } }
</script>

<svelte:document onclick={onDoc} onkeydown={onKey} />

<span class="gp" bind:this={root}>
  <button type="button" class="small" class:has={selected.size} onclick={() => (open = !open)} aria-haspopup="listbox" aria-expanded={open} aria-label={label}>
    {selected.size ? `${selected.size} selected` : empty} <span class="caret">▾</span>
  </button>
  {#if open}
    <div class="pop" role="listbox" aria-label={label}>
      <input type="search" placeholder="Filter…" bind:value={filter} />
      <div class="list">
        {#each shown as n (n)}
          <label class="opt"><input type="checkbox" checked={selected.has(n.toLowerCase())} onchange={() => toggle(n)} /><span class="truncate">{n}</span><span class="cnt">{options[n]?.shows ?? 0}s · {options[n]?.movies ?? 0}f</span></label>
        {:else}
          <div class="muted small" style="padding:.3rem">{names.length ? 'No genre matches.' : 'No genres in the library yet.'}</div>
        {/each}
      </div>
      <div class="foot"><button type="button" class="small ghost" onclick={() => onchange?.([])} disabled={!selected.size}>Clear</button><button type="button" class="small" onclick={() => (open = false)}>Done</button></div>
    </div>
  {/if}
</span>

<style>
  .gp { position: relative; display: inline-block; }
  .has { border-color: var(--info); color: var(--info); }
  .caret { font-size: .7rem; opacity: .7; }
  .pop { position: absolute; z-index: 20; top: calc(100% + 4px); left: 0; width: 280px; max-width: 80vw; background: var(--bg-elev); border: 1px solid var(--border-strong); border-radius: var(--radius-sm); box-shadow: var(--shadow); padding: .4rem; display: flex; flex-direction: column; gap: .3rem; }
  .pop input[type="search"] { width: 100%; min-height: 30px; padding: .25rem .5rem; }
  .list { max-height: 220px; overflow: auto; display: flex; flex-direction: column; }
  .opt { display: flex; align-items: center; gap: .4rem; padding: .2rem .3rem; border-radius: 4px; cursor: pointer; font-size: .85rem; }
  .opt:hover { background: var(--bg-sunken); }
  .opt input { flex: none; }
  .opt .truncate { flex: 1; }
  .cnt { font-size: .7rem; color: var(--fg-muted); white-space: nowrap; }
  .foot { display: flex; justify-content: space-between; }
</style>
