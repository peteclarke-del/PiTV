<script>
  // Editable key -> value rows for a JSON object setting (era weights, genre weights, watershed tables).
  let { value = {}, onchange, keyLabel = 'Key', valueLabel = 'Weight', keyPlaceholder = '', type = 'number', step = '0.05', min = '0', addLabel = 'Add row' } = $props();
  let rows = $state([]);
  // Re-seed from `value` only when the parent hands us a new object (not our own emit()).
  let seeded = null;
  $effect(() => {
    if (value === seeded) return;
    seeded = value;
    rows = Object.entries(value ?? {}).map(([k, v]) => ({ k, v }));
  });
  function emit() {
    const out = {};
    for (const r of rows) if (r.k.trim() !== '') out[r.k.trim()] = type === 'number' ? Number(r.v) : r.v;
    seeded = out;
    onchange?.(out);
  }
  function add() { rows.push({ k: '', v: type === 'number' ? 1 : '20:00' }); }
  function remove(i) { rows.splice(i, 1); emit(); }
</script>

<div class="rows">
  {#if rows.length}
    <div class="hdr tiny muted"><span>{keyLabel}</span><span>{valueLabel}</span><span></span></div>
  {/if}
  {#each rows as r, i (i)}
    <div class="r">
      <input bind:value={r.k} placeholder={keyPlaceholder} onchange={emit} />
      <input {type} {step} {min} bind:value={r.v} onchange={emit} />
      <button class="small ghost" onclick={() => remove(i)} aria-label="Remove">✕</button>
    </div>
  {/each}
  <div><button class="small" onclick={add}>{addLabel}</button></div>
</div>

<style>
  .rows { display: flex; flex-direction: column; gap: .35rem; }
  .hdr, .r { display: grid; grid-template-columns: 1fr 7rem 2rem; gap: .35rem; align-items: center; }
  .r input { min-width: 0; }
</style>
