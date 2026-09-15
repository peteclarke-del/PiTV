<script>
  import AppBadge from '../../components/AppBadge.svelte';
  import { untrack } from 'svelte';
  import { get, post, del, tryApi, confirmApi } from '../../lib/api.js';
  import { changes, clock, toast } from '../../lib/stores.svelte.js';
  import { fmtAgo, fmtEpisode } from '../../lib/format.js';
  import { poll } from '../../lib/poll.svelte.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { num } from '../../lib/util.js';
  import ProgressBar from '../../components/ProgressBar.svelte';
  import StatusBadge from '../../components/StatusBadge.svelte';
  import Drawer from '../../components/Drawer.svelte';
  import DataTable from '../../components/DataTable.svelte';

  let wanted = $state(null);
  let lineupById = $state({});
  let adding = $state(null);       // add-wanted form
  // The line-up only supplies channel numbers for the badges; it is fetched again only after a
  // library change, not on every progress poll.
  let lineupFor = -1;              // changes.library value lineupById was fetched for

  async function load() {
    wanted = (await tryApi(get('/api/wanted'))) ?? wanted ?? [];
    const version = changes.library;
    if (lineupFor !== version && wanted.some((w) => w.lineup_id)) {
      lineupFor = version;
      const entries = await tryApi(get('/api/lineup'));
      if (entries) lineupById = Object.fromEntries(entries.map((e) => [e.id, e]));
      else lineupFor = -1;
    }
  }
  $effect(() => { changes.library; untrack(load); });
  poll(load, 20000); // pitv_content updates progress without an SSE event

  const LIBRARY_TAB = { movie: 'movies', advert: 'adverts', music: 'music' };
  const active = (st) => ['downloading', 'transcoding', 'searching', 'running'].includes(st);
  const pct = (p) => (p > 1 ? p / 100 : p);

  function openAdd(preset = {}) {
    adding = { kind: 'episode', title: '', artist: '', year: '', season: '', episode: '', ref: '', genre: '', ...preset };
  }
  const submitAdd = guard(async () => {
    const b = { kind: adding.kind, title: adding.title.trim(), year: num(adding.year, { min: 1900, max: 2100, int: true }) };
    if (adding.ref.trim()) b.ref = adding.ref.trim();
    if (adding.kind === 'episode') { b.season = num(adding.season, { min: 0, int: true }); b.episode = num(adding.episode, { min: 0, int: true }); }
    if (adding.kind === 'music') { if (adding.genre.trim()) b.genre = adding.genre.trim().toLowerCase(); if (adding.artist.trim()) b.artist = adding.artist.trim(); }
    const r = await tryApi(post('/api/wanted', b), { success: `Queued "${b.title}"` });
    if (r) { adding = null; load(); }
  });
  const retry = (w) => tryApi(post(`/api/wanted/${w.id}/retry`), { success: 'Re-queued' }).then(load);
  async function removeWanted(w) {
    if (await confirmApi(`Remove "${w.title}" from the wanted list?`, { title: 'Remove', okLabel: 'Remove', danger: true }, () => del(`/api/wanted/${w.id}`))) load();
  }
  const scanGaps = guard(async () => {
    const r = await tryApi(post('/api/wanted/scan-gaps'));
    if (r) { toast.success(r.added ? `${r.added} missing episode(s) queued` : 'No gaps found in the shows on disk'); load(); }
  });
  // No default sort: the API already lists queued, then failed, then the rest, newest first.
  const columns = [
    { key: 'title', label: 'Item', get: (w) => [w.show_title, w.title].filter(Boolean).join(' · '), cell: itemCell },
    { key: 'ref', label: 'Source', class: 'small', cell: sourceCell },
    { key: 'status', label: 'Status', cell: statusCell },
    { key: 'attempts', label: 'Attempts', class: 'num' },
    { key: 'created_at', label: 'Added', class: 'small muted nowrap', cell: addedCell },
    { key: 'actions', label: '', class: 'right nowrap', sortable: false, cell: actionsCell },
  ];
</script>

<div class="stack">
  <p class="scope" style="margin:0">PiTV records these requests (by hand, from line-ups, or from gaps in a series); pitv_content fetches them on its next run and delivers them into the cache. Give a URL only when you know exactly where the file is.</p>
  <div class="card">
    <div class="card-title"><h3>Wanted</h3><AppBadge app="content" title="Recorded by PiTV, fetched by pitv_content" />
      <button class="small" onclick={scanGaps} disabled={scanGaps.busy}>Queue missing episodes</button>
      <button class="small primary" onclick={() => openAdd()}>Add wanted</button>
    </div>
    <DataTable id="wanted-list" {columns} rows={wanted} card={false} search="Filter wanted items…"
      empty="Nothing wanted. Add an item or queue missing episodes." />
  </div>
</div>

{#snippet itemCell(w)}<b>{w.show_title ? `${w.show_title} · ` : ''}{w.title}</b>{#if w.auto}<span class="badge" title="Queued automatically">auto</span>{/if}{#if w.lineup_id}<span class="badge info" title="Wanted because of a line-up entry">line-up{lineupById[w.lineup_id]?.channel_number ? ` Ch ${lineupById[w.lineup_id].channel_number}` : ''}</span>{/if}{#if w.transient}<span class="badge warn" title="Fetched for its airing, then removed">transient</span>{/if}
  <div class="tiny muted">{w.kind}{w.year ? ` · ${w.year}` : ''}{w.season != null ? ` · ${fmtEpisode(w.season, w.episode)}` : ''}{w.kind === 'music' && w.artist ? ` · ${w.artist}` : ''}{w.kind === 'music' && w.genre ? ` · ${w.genre}` : ''}</div>{/snippet}
{#snippet sourceCell(w)}{#if w.ref}<div class="tiny muted mono truncate" style="max-width:220px" title={w.ref}>{w.ref}</div>{:else}<span class="muted">search</span>{/if}{/snippet}
{#snippet statusCell(w)}<div style="min-width:160px"><StatusBadge status={w.status} />
  {#if active(w.status)}<ProgressBar value={pct(w.progress ?? 0)} />{/if}
  {#if w.message}<div class="tiny muted">{w.message}</div>{/if}
  {#if w.status === 'done' && w.media_id}<div class="tiny"><a href="#/admin/library/{LIBRARY_TAB[w.kind] ?? 'shows'}">in library</a></div>{/if}</div>{/snippet}
{#snippet addedCell(w)}{fmtAgo(w.created_at, clock.ts)}{/snippet}
{#snippet actionsCell(w)}{#if w.status === 'failed' || w.status === 'done'}<button class="small" onclick={() => retry(w)}>Retry</button>{/if}
  <button class="small danger" onclick={() => removeWanted(w)}>Delete</button>{/snippet}

<Drawer open={!!adding} title="Add wanted item" onclose={() => (adding = null)}>
  {#if adding}
    <div class="stack">
      <p class="scope" style="margin:0"><AppBadge app="content" title="Recorded by PiTV, fetched by pitv_content" /> PiTV records the request; pitv_content fetches it on its next run.</p>
      <label class="field">Kind<select bind:value={adding.kind}><option value="episode">Episode</option><option value="movie">Movie</option><option value="advert">Advert</option><option value="music">Music video</option></select></label>
      <label class="field">Title<input bind:value={adding.title} placeholder={adding.kind === 'episode' ? 'Show title' : adding.kind === 'music' ? 'Artist - Title, e.g. Queen - Radio Ga Ga' : 'Title'} /></label>
      {#if adding.kind === 'music'}
        <label class="field">Artist<input bind:value={adding.artist} placeholder="Queen" /><span class="help">Optional if the title already reads "Artist - Title".</span></label>
        <label class="field">Genre<input bind:value={adding.genre} placeholder="pop" /><span class="help">Optional; decides the genre folder the video is saved under.</span></label>
      {/if}
      <label class="field">Year<input class="narrow" type="number" min="1900" max="2100" bind:value={adding.year} /></label>
      {#if adding.kind === 'episode'}
        <div class="row">
          <label class="field">Season<input class="xnarrow" type="number" min="0" bind:value={adding.season} /></label>
          <label class="field">Episode<input class="xnarrow" type="number" min="0" bind:value={adding.episode} /></label>
        </div>
      {/if}
      <label class="field">URL (optional)<input bind:value={adding.ref} placeholder="https://…" /><span class="help">A specific page or file pitv_content should use instead of searching.</span></label>
    </div>
  {/if}
  {#snippet footer()}
    <button onclick={() => (adding = null)}>Cancel</button>
    <button class="primary" onclick={submitAdd} disabled={submitAdd.busy || !adding?.title?.trim()}>Add</button>
  {/snippet}
</Drawer>
