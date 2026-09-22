<script>
  // Compact multi-select: a button ("Any genre" / "3 selected") opening a popover with a filter and checkboxes.
  // The choices are the genres the library actually holds, counted over `kinds` (episode, movie, music), so a
  // band of music videos is not offered Westerns.
  //
  // `allowNew` also lets a genre nothing carries yet be typed in. Curated material is the reason:
  // somebody's YouTube subscriptions are motorcycling, retro computing and camping, and none of
  // those is a genre any broadcaster's index would have taught the library. Without it the first
  // entry of a subject could never be labelled, so the subject could never exist.
  let { value = [], options = {}, kinds = ['episode', 'movie', 'music'], onchange, empty = 'Any genre',
        label = 'Genres', allowNew = false } = $props();
  let open = $state(false);
  let filter = $state('');
  let root = $state(null);
  const count = (name) => kinds.reduce((n, k) => n + (options[name]?.[k] ?? 0), 0);
  let selected = $derived(new Set((value ?? []).map((v) => String(v).toLowerCase())));
  // Anything already chosen stays listed even if nothing carries it now, so it can be cleared.
  let names = $derived([...new Set([...Object.keys(options).filter((n) => count(n) > 0),
                                    ...(value ?? []).map(String)])].sort((a, b) => a.localeCompare(b)));
  let shown = $derived(names.filter((n) => !filter || n.toLowerCase().includes(filter.toLowerCase())));
  // A typed name that is not already a choice, tidied the way the server would tidy it.
  let fresh = $derived.by(() => {
    const text = filter.trim().replace(/\s+/g, ' ');
    if (!allowNew || !text) return '';
    return names.some((n) => n.toLowerCase() === text.toLowerCase()) ? '' : text;
  });
  function addFresh() {
    if (!fresh) return;
    onchange?.([...(value ?? []), fresh]);
    filter = '';
  }
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
      <input type="search" placeholder={allowNew ? 'Filter, or type a new one…' : 'Filter…'} bind:value={filter}
             onkeydown={(e) => { if (e.key === 'Enter' && fresh) { e.preventDefault(); addFresh(); } }} />
      <div class="list">
        {#each shown as n (n)}
          <label class="opt"><input type="checkbox" checked={selected.has(n.toLowerCase())} onchange={() => toggle(n)} /><span class="truncate">{n}</span><span class="cnt">{count(n)}</span></label>
        {:else}
          <div class="muted small" style="padding:.3rem">{names.length ? 'No genre matches.' : 'No genres in the library yet.'}</div>
        {/each}
      </div>
      {#if fresh}
        <button type="button" class="small" onclick={addFresh}>Add “{fresh}”</button>
      {/if}
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
