<script>
  // Editable key -> value rows for a JSON object setting (era weights, genre weights, watershed
  // tables). With `values="list"` a value is a list of names, typed separated by commas.
  import { splitList } from '../../lib/util.js';
  let { value = {}, onchange, keyLabel = 'Key', valueLabel = 'Weight', keyPlaceholder = '', type = 'number', step = '0.05', min = '0', addLabel = 'Add row', values = '' } = $props();
  const list = $derived(values === 'list');
  let rows = $state([]);
  // Re-seed from `value` only when the parent hands us a new object (not our own emit()).
  let seeded = null;
  $effect(() => {
    if (value === seeded) return;
    seeded = value;
    rows = Object.entries(value ?? {}).map(([k, v]) => ({ k, v: Array.isArray(v) ? v.join(', ') : v }));
  });
  const parsed = (v) => (list ? splitList(String(v ?? '')) : type === 'number' ? Number(v) : v);
  function emit() {
    const out = {};
    for (const r of rows) if (r.k.trim() !== '') out[r.k.trim()] = parsed(r.v);
    seeded = out;
    onchange?.(out);
  }
  function add() { rows.push({ k: '', v: list ? '' : type === 'number' ? 1 : '20:00' }); }
  function remove(i) { rows.splice(i, 1); emit(); }
</script>

<div class="rows" class:list>
  {#each rows as r, i (i)}
    <div class="r">
      <input bind:value={r.k} placeholder={keyPlaceholder || keyLabel} onchange={emit} aria-label={keyLabel} title={keyLabel} />
      {#if list}
        <input bind:value={r.v} onchange={emit} placeholder="names, separated by commas" aria-label={valueLabel} title={valueLabel} />
      {:else}
        <input {type} {step} {min} bind:value={r.v} onchange={emit} aria-label={valueLabel} title={valueLabel} />
      {/if}
      <button class="small ghost" onclick={() => remove(i)} aria-label="Remove">✕</button>
    </div>
  {/each}
  <div class="add"><button class="small" onclick={add}>{addLabel}</button></div>
</div>

<style>
  /* Rows are a name and a short value, so they run in as many columns as fit: thirty genres in
     one column was a screen and a half of rows, each nine tenths empty. */
  .rows { display: grid; gap: .35rem 1.2rem; grid-template-columns: repeat(auto-fill, minmax(min(100%, 17rem), 1fr)); }
  .rows.list { grid-template-columns: repeat(auto-fill, minmax(min(100%, 26rem), 1fr)); }
  .r { display: grid; grid-template-columns: minmax(0, 1fr) 6rem 2rem; gap: .35rem; align-items: center; }
  .list .r { grid-template-columns: minmax(0, 9rem) minmax(0, 1fr) 2rem; }
  .r input { min-width: 0; width: 100%; }
  .add { grid-column: 1 / -1; }
</style>
