<script>
  // The music channel's day as ordered blocks, beside what the library holds to fill them.
  import { onMount } from 'svelte';
  import { get, tryApi } from '../../lib/api.js';
  import { moveItem } from '../../lib/util.js';
  import ChipList from '../../components/ChipList.svelte';
  import DecadePicker from '../../components/DecadePicker.svelte';

  let { value = $bindable([]) } = $props();
  let facets = $state(null);
  onMount(async () => { facets = (await tryApi(get('/api/music/facets'))) ?? null; });

  function add() {
    const last = value.at(-1);
    value.push({ start: last ? last.start : '08:00', name: '', genres: [], decades: [1980], concert: false });
  }
</script>

<div class="music">
  <div class="table-wrap">
    <table class="blocks">
      <thead><tr><th>Start</th><th>Name</th><th>Genres</th><th>Decades</th><th>Concert</th><th></th></tr></thead>
      <tbody>
        {#each value as b, i (i)}
          <tr>
            <td><input type="time" bind:value={b.start} aria-label="Block start" /></td>
            <td><input bind:value={b.name} placeholder="Block name" aria-label="Block name" /></td>
            <td style="min-width:200px"><ChipList value={b.genres ?? []} onchange={(v) => (b.genres = v)} placeholder="genre…" label="Block genres" lower /></td>
            <td style="min-width:220px"><DecadePicker bind:value={b.decades} label="Block decades" /></td>
            <td class="center"><input type="checkbox" checked={!!b.concert} onchange={(e) => (b.concert = e.currentTarget.checked)} aria-label="Concert block" /></td>
            <td class="nowrap"><button class="small ghost" disabled={i === 0} onclick={() => moveItem(value, i, -1)} aria-label="Move up">↑</button><button class="small ghost" disabled={i === value.length - 1} onclick={() => moveItem(value, i, 1)} aria-label="Move down">↓</button><button class="small ghost" onclick={() => value.splice(i, 1)} aria-label="Remove">✕</button></td>
          </tr>
        {:else}
          <tr><td colspan="6" class="muted small">No blocks: the music channel would be empty.</td></tr>
        {/each}
      </tbody>
    </table>
    <div class="mt"><button class="small" onclick={add}>Add block</button></div>
  </div>
  <aside class="facets">
    <h4>In the library</h4>
    {#if facets}
      <div class="small"><b>{facets.concerts}</b> concerts</div>
      <div class="tiny muted mt">Decades</div>
      <div class="row tight">{#each Object.entries(facets.decades) as [d, n] (d)}<span class="chip">{d} <b>{n}</b></span>{:else}<span class="muted small">none</span>{/each}</div>
      <div class="tiny muted mt">Genres</div>
      <div class="row tight">{#each Object.entries(facets.genres) as [g, n] (g)}<span class="chip">{g} <b>{n}</b></span>{:else}<span class="muted small">none</span>{/each}</div>
    {:else}<div class="skeleton" style="height:60px"></div>{/if}
  </aside>
</div>

<style>
  .music { display: grid; gap: 1rem; grid-template-columns: 1fr; }
  @media (min-width: 1000px) { .music { grid-template-columns: 1fr 260px; align-items: start; } }
  .blocks td { padding: .3rem; vertical-align: top; }
  .blocks input[type="time"] { min-width: 6.5rem; }
  .facets { background: var(--bg-sunken); border-radius: var(--radius-sm); padding: .75rem; }
  .facets h4 { margin: 0 0 .4rem; font-size: .85rem; }
  .row.tight { gap: .25rem; }
  .row.tight .chip { font-size: .72rem; padding: .1rem .4rem; }
</style>
