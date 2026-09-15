<script>
  // Toggle chips for decades; `value` is a sorted list of decade start years.
  let { value = $bindable([]), decades = [1930, 1940, 1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020], label = 'Decades' } = $props();
  const toggle = (d) => (value = value.includes(d) ? value.filter((x) => x !== d) : [...value, d].sort((a, b) => a - b));
</script>

<div class="decades" role="group" aria-label={label}>
  {#each decades as d (d)}
    <label class="dec" class:on={value.includes(d)}><input type="checkbox" checked={value.includes(d)} onchange={() => toggle(d)} />{d}s</label>
  {/each}
</div>

<style>
  .decades { display: flex; flex-wrap: wrap; gap: .2rem; }
  .dec { font-size: .72rem; padding: .1rem .4rem; border: 1px solid var(--border); border-radius: 999px; cursor: pointer; user-select: none; }
  .dec input { position: absolute; opacity: 0; width: 0; height: 0; margin: 0; }
  .dec:has(input:focus-visible) { outline: 2px solid var(--info); outline-offset: 1px; }
  .dec.on { background: var(--accent); color: #fff; border-color: var(--accent); }
</style>
