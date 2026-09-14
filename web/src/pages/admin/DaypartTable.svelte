<script>
  let { rows = $bindable([]) } = $props();
  function add() {
    const last = rows.at(-1);
    rows.push({ name: '', start: last ? last.start : '08:00', tv: 1, movie: 0.5, kids: 0.5, sport: 0.2, max_minutes: null });
  }
</script>

<div class="table-wrap">
  <table class="dp">
    <thead><tr><th>Start</th><th>Name</th><th>TV</th><th>Movie</th><th>Kids</th><th>Sport</th><th>Max min</th><th></th></tr></thead>
    <tbody>
      {#each rows as r, i (i)}
        <tr>
          <td><input type="time" bind:value={r.start} /></td>
          <td><input bind:value={r.name} placeholder="Name" /></td>
          <td><input type="number" step="0.1" min="0" bind:value={r.tv} /></td>
          <td><input type="number" step="0.1" min="0" bind:value={r.movie} /></td>
          <td><input type="number" step="0.1" min="0" bind:value={r.kids} /></td>
          <td><input type="number" step="0.1" min="0" value={r.sport ?? 0} onchange={(e) => (r.sport = Number(e.currentTarget.value))} /></td>
          <td><input type="number" min="0" placeholder="–" value={r.max_minutes ?? ''} onchange={(e) => (r.max_minutes = e.currentTarget.value === '' ? null : Number(e.currentTarget.value))} /></td>
          <td class="nowrap">
            <button class="small ghost" disabled={i === 0} onclick={() => { const [x] = rows.splice(i, 1); rows.splice(i - 1, 0, x); }} aria-label="Move up">↑</button>
            <button class="small ghost" onclick={() => rows.splice(i, 1)} aria-label="Remove">✕</button>
          </td>
        </tr>
      {:else}
        <tr><td colspan="8" class="muted small">No dayparts.</td></tr>
      {/each}
    </tbody>
  </table>
</div>
<div class="mt"><button class="small" onclick={add}>Add daypart</button></div>

<style>
  .dp input { width: 100%; min-width: 4.5rem; }
  .dp td { padding: .25rem .3rem; }
  .dp td:nth-child(2) input { min-width: 8rem; }
</style>
