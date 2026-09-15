<script>
  // Editable list of short strings shown as chips; type and press Enter or comma to add.
  let { value = [], onchange, placeholder = 'Add…', lower = false } = $props();
  let text = $state('');
  function add() {
    const parts = text.split(',').map((t) => t.trim()).filter(Boolean).map((t) => (lower ? t.toLowerCase() : t));
    if (!parts.length) { text = ''; return; }
    const next = [...value];
    for (const p of parts) if (!next.includes(p)) next.push(p);
    text = '';
    onchange?.(next);
  }
  function remove(i) { onchange?.(value.filter((_, j) => j !== i)); }
  function onkey(e) {
    if (e.key === 'Enter' || e.key === ',') { e.preventDefault(); add(); }
    else if (e.key === 'Backspace' && !text && value.length) remove(value.length - 1);
  }
</script>

<div class="chips">
  {#each value as v, i (v)}<span class="chip">{v}<button type="button" onclick={() => remove(i)} aria-label="Remove {v}">✕</button></span>{/each}
  <input bind:value={text} {placeholder} onkeydown={onkey} onblur={add} />
</div>

<style>
  .chips { display: flex; flex-wrap: wrap; gap: .3rem; align-items: center; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); padding: .25rem .4rem; background: var(--bg-elev); min-height: 34px; }
  .chips input { border: 0; min-height: 0; padding: .15rem .2rem; flex: 1; min-width: 6rem; background: transparent; }
  .chips input:focus { outline: none; }
</style>
