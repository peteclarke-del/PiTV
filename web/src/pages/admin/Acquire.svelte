<script>
  import { untrack, onMount } from 'svelte';
  import { get, post, del, tryApi } from '../../lib/api.js';
  import { changes, confirm, player, clock, toast } from '../../lib/stores.svelte.js';
  import { fmtBytes, fmtDuration, fmtAgo } from '../../lib/format.js';
  import ProgressBar from '../../components/ProgressBar.svelte';
  import Drawer from '../../components/Drawer.svelte';

  let wanted = $state(null);
  let queue = $state(null);
  let adding = $state(null);       // add-wanted form
  let search = $state({ q: '', year: '', results: null, busy: false });
  let files = $state(null);        // {ref, title, year, list}
  let fileFilter = $state('');
  let shownFiles = $derived((files?.list ?? []).filter((f) => !fileFilter || f.name.toLowerCase().includes(fileFilter.toLowerCase())));
  let pickKind = $state('movie');
  let busy = $state(false);

  async function load() {
    try { [wanted, queue] = await Promise.all([get('/api/wanted'), get('/api/transcode')]); } catch { wanted = wanted ?? []; queue = queue ?? []; }
  }
  $effect(() => { changes.library; untrack(load); }); // eslint-disable-line no-unused-expressions
  // The acquisition worker lives in the player; its progress arrives with player state. Refetch when it changes.
  let lastAcq = '';
  $effect(() => {
    const c = player.state.acquire?.current;
    const key = c ? `${c.kind}:${c.id}:${c.status}:${Math.round((c.progress ?? 0) * 20)}` : 'idle';
    if (key !== lastAcq) { lastAcq = key; untrack(load); }
  });
  onMount(() => { const t = setInterval(load, 20000); return () => clearInterval(t); });

  const active = (st) => ['downloading', 'transcoding', 'searching', 'running'].includes(st);
  const badge = (st) => (st === 'done' ? 'ok' : st === 'failed' ? 'danger' : active(st) ? 'info' : '');
  const pct = (p) => (p > 1 ? p / 100 : p);

  function openAdd(preset = {}) {
    adding = { kind: 'episode', title: '', year: '', season: '', episode: '', provider: 'auto', ref: '', genre: '', ...preset };
  }
  async function submitAdd() {
    const b = { kind: adding.kind, title: adding.title, provider: adding.provider, ref: adding.ref || null,
                year: adding.year === '' ? null : Number(adding.year) };
    if (adding.kind === 'episode') { b.season = adding.season === '' ? null : Number(adding.season); b.episode = adding.episode === '' ? null : Number(adding.episode); }
    if (adding.kind === 'music' && adding.genre) b.genre = adding.genre.trim().toLowerCase();
    busy = true;
    const r = await tryApi(post('/api/wanted', b), { success: `Queued "${b.title}"` });
    busy = false;
    if (r) { adding = null; load(); }
  }
  const retry = (w) => tryApi(post(`/api/wanted/${w.id}/retry`), { success: 'Re-queued' }).then(load);
  async function removeWanted(w) {
    if (!(await confirm(`Remove "${w.title}" from the wanted list?`, { title: 'Remove', okLabel: 'Remove', danger: true }))) return;
    if (await tryApi(del(`/api/wanted/${w.id}`))) load();
  }
  async function scanGaps() {
    const r = await tryApi(post('/api/wanted/scan-gaps'));
    if (r) { toast.success(r.added ? `${r.added} missing episode(s) queued` : 'No gaps found in the shows on disk'); load(); }
  }
  async function doSearch(e) {
    e?.preventDefault();
    if (!search.q.trim()) return;
    search.busy = true; files = null;
    search.results = (await tryApi(get('/api/archive/search', { q: search.q.trim(), year: search.year || undefined }))) ?? null;
    search.busy = false;
  }
  async function openFiles(r) {
    files = { ref: r.ref, title: r.title, year: r.year, list: null }; fileFilter = '';
    files.list = (await tryApi(get('/api/archive/files', { identifier: r.ref }))) ?? [];
  }
  async function pickFile(f) {
    const b = { kind: pickKind, title: files.title, year: files.year ?? null, provider: 'archive', ref: `${files.ref}/${f.name}` };
    if (pickKind === 'episode') { openAdd(b); return; } // let the user fill in season/episode
    const r = await tryApi(post('/api/wanted', b), { success: `Queued ${f.name}` });
    if (r) { files = null; load(); }
  }
  async function removeTranscode(t) {
    if (await tryApi(del(`/api/transcode/${t.id}`))) load();
  }
  async function queueAllSoftware() {
    if (!(await confirm('Queue every software-decoded episode and film for transcoding to H.264? This runs in the transcode hours set under Weighting.', { title: 'Queue transcodes', okLabel: 'Queue' }))) return;
    const r = await tryApi(post('/api/transcode', { all_software: true }));
    if (r) { toast.success(`${r.added} file(s) queued`); load(); }
  }
  let acq = $derived(player.state.acquire?.current ?? null);
</script>

<div class="stack">
  <div class="note">Missing programmes are fetched by the player's background worker. Only <b>archive.org</b> items and <b>explicit URLs</b> are supported as sources; enable acquisition and set the hours under Weighting → Acquisition.</div>
  {#if !player.state.online}<div class="warn-box">The player is offline, so nothing will be downloaded or transcoded until it starts.</div>
  {:else if acq}<div class="small"><span class="badge info">{acq.kind}</span> Working on <b>{acq.title ?? `#${acq.id}`}</b>: {acq.status}{acq.message ? ` · ${acq.message}` : ''}</div>{/if}

  <div class="card">
    <div class="card-title"><h3>Wanted</h3>
      <button class="small" onclick={scanGaps}>Queue missing episodes</button>
      <button class="small primary" onclick={() => openAdd()}>Add wanted</button>
    </div>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Item</th><th>Source</th><th>Status</th><th>Attempts</th><th>Added</th><th></th></tr></thead>
        <tbody>
          {#each wanted ?? [] as w (w.id)}
            <tr>
              <td><b>{w.show_title ? `${w.show_title} · ` : ''}{w.title}</b>{#if w.auto}<span class="badge" title="Queued automatically">auto</span>{/if}
                <div class="tiny muted">{w.kind}{w.year ? ` · ${w.year}` : ''}{w.season != null ? ` · S${String(w.season).padStart(2, '0')}E${String(w.episode ?? 0).padStart(2, '0')}` : ''}{w.kind === 'music' && w.artist ? ` · ${w.artist}` : ''}{w.kind === 'music' && w.genre ? ` · ${w.genre}` : ''}</div></td>
              <td class="small"><div>{w.provider}</div>{#if w.ref}<div class="tiny muted mono truncate" style="max-width:220px" title={w.ref}>{w.ref}</div>{/if}</td>
              <td style="min-width:160px"><span class="badge {badge(w.status)}">{w.status}</span>
                {#if active(w.status)}<ProgressBar value={pct(w.progress ?? 0)} />{/if}
                {#if w.message}<div class="tiny muted">{w.message}</div>{/if}
                {#if w.status === 'done' && w.media_id}<div class="tiny"><a href="#/admin/library/{w.kind === 'movie' ? 'movies' : w.kind === 'advert' ? 'adverts' : ''}">in library</a></div>{/if}</td>
              <td class="num">{w.attempts}</td>
              <td class="small muted nowrap">{fmtAgo(w.created_at, clock.ts)}</td>
              <td class="right nowrap">
                {#if w.status === 'failed' || w.status === 'done'}<button class="small" onclick={() => retry(w)}>Retry</button>{/if}
                <button class="small danger" onclick={() => removeWanted(w)}>Delete</button>
              </td>
            </tr>
          {:else}
            <tr><td colspan="6" class="empty">{wanted ? 'Nothing wanted. Add an item, search archive.org, or queue missing episodes.' : 'Loading…'}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>

  <div class="card">
    <div class="card-title"><h3>Search archive.org</h3></div>
    <form class="inline-form" onsubmit={doSearch}>
      <label class="field" style="flex:1;min-width:200px">Title<input bind:value={search.q} placeholder="e.g. Blake's 7" /></label>
      <label class="field">Year<input class="narrow" type="number" min="1900" max="2100" bind:value={search.year} placeholder="any" /></label>
      <label class="field">Add as<select bind:value={pickKind}><option value="movie">Movie</option><option value="episode">Episode</option><option value="advert">Advert</option><option value="music">Music video</option></select></label>
      <button class="primary" type="submit" disabled={search.busy || !search.q.trim()}>{search.busy ? 'Searching…' : 'Search'}</button>
    </form>
    {#if search.results}
      <div class="results mt">
        <ul class="list">
          {#each search.results as r (r.ref)}
            <li class:sel={files?.ref === r.ref}>
              <button class="ghost item" onclick={() => openFiles(r)}>
                <span class="truncate"><b>{r.title}</b> <span class="muted small">{r.year ?? ''}</span></span>
                <span class="tiny muted">{r.ref} · {fmtBytes(r.size)}</span>
                {#if r.detail}<span class="tiny muted truncate">{r.detail}</span>{/if}
              </button>
            </li>
          {:else}
            <li class="muted small">No results.</li>
          {/each}
        </ul>
        {#if files}
          <div class="files">
            <div class="row"><b class="truncate" style="flex:1">{files.title}</b><button class="small ghost" onclick={() => (files = null)}>✕</button></div>
            {#if !files.list}<div class="skeleton" style="height:60px"></div>
            {:else}
              {#if files.list.length > 12}<input type="search" placeholder="Filter {files.list.length} files…" bind:value={fileFilter} class="mb" style="width:100%" />{/if}
              <ul class="list">
                {#each shownFiles as f (f.name)}
                  <li><button class="ghost item" onclick={() => pickFile(f)} title="Add to wanted list">
                    <span class="truncate mono small">{f.name}</span><span class="tiny muted">{f.format ?? ''} · {fmtBytes(f.size)}</span></button></li>
                {:else}
                  <li class="muted small">No video files listed.</li>
                {/each}
              </ul>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
  </div>

  <div class="card">
    <div class="card-title"><h3>Transcode queue</h3><button class="small" onclick={queueAllSoftware}>Queue all software-decoded files</button></div>
    <p class="small muted">Files the Pi cannot hardware-decode are re-encoded to H.264 during the transcode hours; the schedule then plays the copy.</p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Title</th><th>Source</th><th>Status</th><th></th></tr></thead>
        <tbody>
          {#each queue ?? [] as t (t.id)}
            <tr>
              <td><b>{t.title}</b>{#if t.transcoded_path}<div class="tiny muted mono truncate" style="max-width:320px">{t.transcoded_path}</div>{/if}</td>
              <td class="small"><span class="mono">{t.vcodec ?? '?'}</span>{t.height ? ` ${t.height}p` : ''} · {fmtDuration(t.duration)}</td>
              <td style="min-width:160px"><span class="badge {badge(t.status)}">{t.status}</span>{#if active(t.status)}<ProgressBar value={pct(t.progress ?? 0)} />{/if}{#if t.message}<div class="tiny muted">{t.message}</div>{/if}</td>
              <td class="right"><button class="small danger" onclick={() => removeTranscode(t)}>Delete</button></td>
            </tr>
          {:else}
            <tr><td colspan="4" class="empty">{queue ? 'Queue is empty.' : 'Loading…'}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  </div>
</div>

<Drawer open={!!adding} title="Add wanted item" onclose={() => (adding = null)}>
  {#if adding}
    <div class="stack">
      <label class="field">Kind<select bind:value={adding.kind}><option value="episode">Episode</option><option value="movie">Movie</option><option value="advert">Advert</option><option value="music">Music video</option></select></label>
      <label class="field">Title<input bind:value={adding.title} placeholder={adding.kind === 'episode' ? 'Show title' : adding.kind === 'music' ? 'Artist - Title, e.g. Queen - Radio Ga Ga' : 'Title'} /></label>
      {#if adding.kind === 'music'}
        <label class="field">Genre<input bind:value={adding.genre} placeholder="pop" list="music-genres" /><span class="help">Optional; decides the genre folder the video is saved under.</span></label>
      {/if}
      <label class="field">Year<input class="narrow" type="number" min="1900" max="2100" bind:value={adding.year} /></label>
      {#if adding.kind === 'episode'}
        <div class="row">
          <label class="field">Season<input class="xnarrow" type="number" min="0" bind:value={adding.season} /></label>
          <label class="field">Episode<input class="xnarrow" type="number" min="0" bind:value={adding.episode} /></label>
        </div>
      {/if}
      <label class="field">Provider
        <select bind:value={adding.provider}><option value="auto">Auto (search archive.org)</option><option value="archive">archive.org item</option><option value="url">Direct URL</option></select>
        <span class="help">Auto searches archive.org by title and year.</span>
      </label>
      {#if adding.provider !== 'auto'}
        <label class="field">{adding.provider === 'url' ? 'URL' : 'archive.org identifier or identifier/filename'}
          <input bind:value={adding.ref} placeholder={adding.provider === 'url' ? 'https://…/file.mp4' : 'identifier/file.mp4'} />
        </label>
      {/if}
    </div>
  {/if}
  {#snippet footer()}
    <button onclick={() => (adding = null)}>Cancel</button>
    <button class="primary" onclick={submitAdd} disabled={busy || !adding?.title || (adding?.provider === 'url' && !/^https?:\/\//.test(adding.ref))}>Add</button>
  {/snippet}
</Drawer>

<style>
  .results { display: grid; gap: 1rem; grid-template-columns: 1fr; }
  @media (min-width: 800px) { .results { grid-template-columns: 1fr 1fr; align-items: start; } }
  .list { list-style: none; margin: 0; padding: 0; max-height: 380px; overflow: auto; display: flex; flex-direction: column; gap: .2rem; }
  .item { width: 100%; display: flex; flex-direction: column; align-items: flex-start; gap: .1rem; text-align: left; padding: .4rem .5rem; min-height: 0; }
  .item > span { max-width: 100%; }
  li.sel .item { background: var(--bg-sunken); }
  .files { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: .6rem; }
</style>
