<script>
  // Toggle chips for decades; `value` is a sorted list of decade start years. `decades` is what the
  // library holds; with none known yet (an empty catalogue) the usual span is offered instead.
  const SPAN = [1930, 1940, 1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020];
  let { value = $bindable([]), decades = [], label = 'Decades' } = $props();
  // Whatever the caller hands over, work in numbers: a list held as JSON text would otherwise
  // spread into single characters and fill the row with nonsense.
  const years = (v) => (Array.isArray(v) ? v : []).map(Number).filter((n) => Number.isFinite(n));
  // Anything already chosen stays listed even if nothing carries it now, so it can be cleared.
  let chosen = $derived(years(value));
  let shown = $derived([...new Set([...(years(decades).length ? years(decades) : SPAN), ...chosen])].sort((a, b) => a - b));
  const toggle = (d) => (value = chosen.includes(d) ? chosen.filter((x) => x !== d) : [...chosen, d].sort((a, b) => a - b));
</script>

<div class="decades" role="group" aria-label={label}>
  {#each shown as d (d)}
    <label class="dec" class:on={chosen.includes(d)}><input type="checkbox" checked={chosen.includes(d)} onchange={() => toggle(d)} />{d}s</label>
  {/each}
</div>

<style>
  .decades { display: flex; flex-wrap: wrap; gap: .2rem; }
  .dec { font-size: .72rem; padding: .1rem .4rem; border: 1px solid var(--border); border-radius: 999px; cursor: pointer; user-select: none; }
  .dec input { position: absolute; opacity: 0; width: 0; height: 0; margin: 0; }
  .dec:has(input:focus-visible) { outline: 2px solid var(--info); outline-offset: 1px; }
  .dec.on { background: var(--accent); color: #fff; border-color: var(--accent); }
</style>
