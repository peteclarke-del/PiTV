<script>
  // Search shows (pick an episode) or movies for the schedule editor. onpick({media_id, label, duration}).
  import { get, tryApi } from '../../lib/api.js';
  import { fmtDuration, fmtEpisode } from '../../lib/format.js';
  import { debounce } from '../../lib/util.js';
  import Modal from '../../components/Modal.svelte';

  let { open = false, onclose, onpick } = $props();
  let q = $state('');
  let kind = $state('programme');
  let results = $state([]);
  let loading = $state(false);
  let episodeChoice = $state({}); // show id -> chosen episode id

  async function search() {
    loading = true;
    results = (await tryApi(get('/api/library/search', { q, kind, limit: 30 }))) ?? [];
    loading = false;
  }
  const searchSoon = debounce(search, 250);
  function input(v) { q = v; searchSoon(); }
  $effect(() => { if (open) { q = ''; results = []; search(); } });

  function pickEpisode(show) {
    const id = Number(episodeChoice[show.id] ?? show.episodes[0]?.id);
    const ep = show.episodes.find((e) => e.id === id);
    if (!ep) return;
    onpick?.({ media_id: ep.id, label: `${show.title} · ${fmtEpisode(ep.season, ep.episode)} ${ep.title}`, duration: ep.duration });
  }
</script>

<Modal {open} title="Choose a programme" {onclose} width="560px">
  <div class="stack">
    <div class="row">
      <input type="search" placeholder="Search shows, movies and music…" value={q} oninput={(e) => input(e.currentTarget.value)} style="flex:1" />
      <select bind:value={kind} onchange={search} aria-label="Kind"><option value="programme">All</option><option value="tv">Shows</option><option value="movie">Movies</option><option value="music">Music</option></select>
    </div>
    <ul class="results" class:loading>
      {#each results as r (`${r.type}-${r.id}`)}
        <li>
          {#if r.type === 'show'}
            <div class="line">
              <span class="badge info">Show</span><b class="truncate">{r.title}</b><span class="muted small">{r.year ?? ''}</span>
            </div>
            <div class="line">
              <select value={episodeChoice[r.id] ?? r.episodes[0]?.id} aria-label="Episode" onchange={(e) => (episodeChoice[r.id] = e.currentTarget.value)} style="flex:1;min-width:0">
                {#each r.episodes as e (e.id)}
                  <option value={e.id}>{fmtEpisode(e.season, e.episode)} {e.title} ({fmtDuration(e.duration)})</option>
                {/each}
              </select>
              <button class="small primary" onclick={() => pickEpisode(r)} disabled={!r.episodes.length}>Pick</button>
            </div>
          {:else if r.type === 'music'}
            <div class="line">
              <span class="badge info">{r.concert ? 'Concert' : 'Music'}</span><b class="truncate">{r.title}</b><span class="muted small nowrap">{r.year ?? ''} · {fmtDuration(r.duration)}</span>
              <button class="small primary" onclick={() => onpick?.({ media_id: r.id, label: `${r.title}${r.year ? ` (${r.year})` : ''}`, duration: r.duration })}>Pick</button>
            </div>
          {:else}
            <div class="line">
              <span class="badge warn">Movie</span><b class="truncate">{r.title}</b><span class="muted small nowrap">{r.year ?? ''} {r.certificate ?? ''} · {fmtDuration(r.duration)}</span>
              <button class="small primary" onclick={() => onpick?.({ media_id: r.id, label: `${r.title} (${r.year ?? '?'})`, duration: r.duration })}>Pick</button>
            </div>
          {/if}
        </li>
      {:else}
        <li class="muted small">{loading ? 'Searching…' : 'No matches.'}</li>
      {/each}
    </ul>
  </div>
</Modal>

<style>
  .results { list-style: none; margin: 0; padding: 0; max-height: 55vh; overflow: auto; display: flex; flex-direction: column; gap: .5rem; }
  .results.loading { opacity: .6; }
  .results li { padding: .4rem 0; border-bottom: 1px solid var(--border); display: flex; flex-direction: column; gap: .3rem; }
  .line { display: flex; align-items: center; gap: .5rem; min-width: 0; }
  .line b { flex: 1; }
</style>
