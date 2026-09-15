<script>
  // What PiTV asks pitv_content for. pitv_content's reports back are listed under Last run on the same tab.
  import { onMount } from 'svelte';
  import { get, tryApi } from '../../lib/api.js';
  import { fmtDateTime } from '../../lib/format.js';
  import AppBadge from '../../components/AppBadge.svelte';
  import { downloadJson } from '../../lib/util.js';

  let manifest = $state(null);
  let manifestDays = $state(1);
  let manifestBusy = $state(false);
  async function loadManifest() {
    manifestBusy = true;
    manifest = (await tryApi(get('/api/content/manifest', { days: manifestDays }))) ?? manifest;
    manifestBusy = false;
  }
  const downloadManifest = () => downloadJson(manifest, `pitv-manifest-${new Date(manifest.generated_ts * 1000).toISOString().slice(0, 10)}.json`);
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
  <div class="card-title"><h3>Request manifest</h3><AppBadge app="pitv" />
    <select bind:value={manifestDays} onchange={loadManifest} aria-label="Days">{#each [1, 2, 3, 7] as d (d)}<option value={d}>{d} day{d > 1 ? 's' : ''}</option>{/each}</select>
    <button class="small" onclick={loadManifest} disabled={manifestBusy}>Refresh</button>
    <button class="small primary" onclick={downloadManifest} disabled={!manifest}>Download manifest JSON</button>
  </div>
  <p class="scope">PiTV publishes this for pitv_content: every file the schedule needs through the next broadcast day, to copy, transcode or fetch into the cache.</p>
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
</div>
