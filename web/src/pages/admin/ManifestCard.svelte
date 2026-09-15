<script>
  import { onMount } from 'svelte';
  import { get, tryApi } from '../../lib/api.js';
  import { clock } from '../../lib/stores.svelte.js';
  import { fmtAgo, fmtDateTime } from '../../lib/format.js';

  let manifest = $state(null);
  let manifestDays = $state(1);
  let manifestBusy = $state(false);
  let contentRuns = $state([]);
  async function loadManifest() {
    manifestBusy = true;
    manifest = (await tryApi(get('/api/content/manifest', { days: manifestDays }))) ?? manifest;
    contentRuns = ((await tryApi(get('/api/runs', { limit: 30 }))) ?? []).filter((r) => r.kind === 'content').slice(0, 5);
    manifestBusy = false;
  }
  function downloadManifest() {
    if (!manifest) return;
    const blob = new Blob([JSON.stringify(manifest, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `pitv-manifest-${new Date(manifest.generated_ts * 1000).toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a); a.click(); a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  let manifestStats = $derived.by(() => {
    if (!manifest) return null;
    const items = manifest.items ?? [];
    const by = {};
    for (const it of items) by[it.action] = (by[it.action] ?? 0) + 1;
    return { total: items.length, by, cached: items.filter((i) => i.already_cached).length, wanted: (manifest.wanted ?? []).length, shortfalls: (manifest.shortfalls ?? []).length };
  });
  onMount(loadManifest);
</script>

  <div class="card">
    <div class="card-title"><h3>Content manifest</h3>
      <select bind:value={manifestDays} onchange={loadManifest} aria-label="Days">{#each [1, 2, 3, 7] as d (d)}<option value={d}>{d} day{d > 1 ? 's' : ''}</option>{/each}</select>
      <button class="small" onclick={loadManifest} disabled={manifestBusy}>Refresh</button>
      <button class="small primary" onclick={downloadManifest} disabled={!manifest}>Download manifest JSON</button>
    </div>
    <p class="small muted">What the content provider (pitv_content or the built-in copier) needs to put in the cache for the coming schedule.</p>
    {#if manifest && manifestStats}
      <div class="stats">
        <div class="stat"><b>{manifestStats.total}</b><span>Items</span></div>
        <div class="stat"><b>{manifestStats.by.copy ?? 0}</b><span>Copy</span></div>
        <div class="stat"><b>{manifestStats.by.transcode ?? 0}</b><span>Transcode</span></div>
        <div class="stat"><b>{manifestStats.cached}</b><span>Already cached</span></div>
        <div class="stat"><b>{manifestStats.wanted}</b><span>Wanted</span></div>
        {#if manifestStats.shortfalls}<div class="stat"><b>{manifestStats.shortfalls}</b><span>Shortfalls</span></div>{/if}
      </div>
      <p class="tiny muted mt">Generated {fmtDateTime(manifest.generated_ts)} · horizon {fmtDateTime(manifest.horizon_ts)} · cache <span class="mono">{manifest.cache_dir || '(unset)'}</span> · acquire <span class="mono">{manifest.acquire_dir || '(unset)'}</span>{manifest.profile ? ` · profile ${manifest.profile.width}×${manifest.profile.height} ${manifest.profile.vcodec}` : ''}</p>
      {#if manifest.note}<p class="note small">{manifest.note}</p>{/if}
      <div class="table-wrap">
        <table>
          <thead><tr><th>Title</th><th>Kind</th><th>Action</th><th>First air</th><th>Channels</th><th>Cached</th></tr></thead>
          <tbody>
            {#each manifest.items.slice(0, 50) as it (it.media_id ?? it.target)}
              <tr>
                <td><b>{it.title}</b><div class="tiny muted truncate" style="max-width:320px" title={it.relpath}>{it.relpath}</div></td>
                <td class="small">{it.kind}</td>
                <td><span class="badge {it.action === 'transcode' ? 'warn' : 'info'}">{it.action}</span></td>
                <td class="small nowrap">{fmtDateTime(it.first_air_ts)}</td>
                <td class="small">{(it.channels ?? []).join(', ')}</td>
                <td>{#if it.already_cached}<span class="badge ok">yes</span>{:else}<span class="muted small">no</span>{/if}</td>
              </tr>
            {:else}
              <tr><td colspan="6" class="empty">Nothing to fetch for this window.</td></tr>
            {/each}
          </tbody>
        </table>
        {#if manifest.items.length > 50}<p class="tiny muted">Showing the first 50 of {manifest.items.length} items; download the JSON for the full list.</p>{/if}
      </div>
    {:else}
      <div class="skeleton" style="height:80px"></div>
    {/if}
    <h4 class="mt">pitv_content reports</h4>
    {#if contentRuns.length}
      <ul class="runs">{#each contentRuns as r (r.id)}<li><span class="badge {r.status === 'ok' ? 'ok' : r.status === 'running' ? 'info' : r.status === 'warning' ? 'warn' : 'danger'}">{r.status}</span><span class="small">{r.summary || '–'}</span><span class="tiny muted nowrap">{fmtAgo(r.started_at, clock.ts)}</span></li>{/each}</ul>
    {:else}<p class="muted small">No reports yet; the pitv_content tool posts one after each overnight run.</p>{/if}
  </div>


<style>
  .runs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .4rem; }
  .runs li { display: flex; gap: .5rem; align-items: baseline; }
  .runs li span:nth-child(2) { flex: 1; }
</style>
