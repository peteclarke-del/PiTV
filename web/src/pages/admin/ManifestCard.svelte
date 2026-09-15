<script>
  // What PiTV asks pitv_content for. pitv_content's reports back are listed under Last run on the same tab.
  import { onMount } from 'svelte';
  import { get, tryApi } from '../../lib/api.js';
  import { fmtDateTime, fmtProfile } from '../../lib/format.js';
  import AppBadge from '../../components/AppBadge.svelte';
  import DataTable from '../../components/DataTable.svelte';
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
  const title = (it) => (it.show_title ? `${it.show_title} · ${it.title}` : it.title);
  const columns = [
    { key: 'title', label: 'Title', get: title, cell: titleCell },
    { key: 'kind', label: 'Kind', class: 'small' },
    { key: 'action', label: 'Action', cell: actionCell },
    { key: 'first_air_ts', label: 'First air', class: 'small nowrap', cell: airCell },
    { key: 'channels', label: 'Channels', class: 'small', get: (it) => (it.channels ?? []).join(', ') },
    { key: 'already_cached', label: 'Cached', get: (it) => !!it.already_cached, cell: cachedCell },
  ];
  onMount(loadManifest);
</script>

<div class="card">
  <div class="card-title"><h3>Request manifest</h3><AppBadge app="pitv" />
    <select bind:value={manifestDays} onchange={loadManifest} aria-label="Days">{#each [1, 2, 3, 7] as d (d)}<option value={d}>{d} day{d > 1 ? 's' : ''}</option>{/each}</select>
    <button class="small" onclick={loadManifest} disabled={manifestBusy}>Refresh</button>
    <button class="small primary" onclick={downloadManifest} disabled={!manifest}>Download manifest JSON</button>
  </div>
  <p class="scope">PiTV publishes this for pitv_content: every file the schedule needs through the next broadcast day, to copy, transcode or fetch into the cache.</p>
  {#if manifest?.profile}<p class="small">Quality: {fmtProfile(manifest.profile)}. <a href="#/admin/settings/screen">Change the screen</a></p>{/if}
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
    <DataTable id="manifest-items" {columns} rows={manifest.items ?? []} key={(it) => it.request_id} search="Filter items…"
      sort={{ key: 'first_air_ts', dir: 'asc' }} card={false} empty="Nothing to fetch for this window." />
  {:else}
    <div class="skeleton" style="height:80px"></div>
  {/if}
</div>

{#snippet titleCell(it)}<b>{title(it)}</b>{#if it.uid}<div class="tiny muted truncate" style="max-width:320px" title={it.uid}>{it.uid}</div>{/if}{/snippet}
{#snippet actionCell(it)}<span class="badge {it.action === 'transcode' ? 'warn' : 'info'}">{it.action}</span>{/snippet}
{#snippet airCell(it)}{fmtDateTime(it.first_air_ts)}{/snippet}
{#snippet cachedCell(it)}{#if it.already_cached}<span class="badge ok">yes</span>{:else}<span class="muted small">no</span>{/if}{/snippet}
