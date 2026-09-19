<script>
  import { untrack } from 'svelte';
  import { get, put, tryApi } from '../../lib/api.js';
  import { fmtDuration, fmtBytes, fmtDateTime, CERTIFICATES, PROGRAMME_TYPES, programmeTypeLabel } from '../../lib/format.js';
  import { num, splitList } from '../../lib/util.js';
  import { guard } from '../../lib/guard.svelte.js';
  import AppBadge from '../../components/AppBadge.svelte';
  import Drawer from '../../components/Drawer.svelte';
  import Availability from '../../components/Availability.svelte';

  let { id, channels = [], onclose, onsaved } = $props();
  let item = $state(null);
  let form = $state(null);

  async function load() {
    const m = await tryApi(get(`/api/media/${id}`));
    if (!m) { onclose?.(); return; }
    item = m;
    form = { title: m.title ?? '', year: m.year ?? '', certificate: m.certificate ?? '', genres: (m.genres ?? []).join(', '),
             plot: m.plot ?? '', excluded: !!m.excluded, programme_type: m.programme_type_set ? m.programme_type : '', artist: m.artist ?? '', concert: !!m.concert, family_safe: m.family_safe !== 0,
             home_channel_id: m.home_channel_id ?? '' };
  }
  $effect(() => { id; untrack(load); });

  const save = guard(async () => {
    const genres = splitList(form.genres);
    const body = { title: form.title, year: num(form.year, { min: 1900, max: 2100, int: true }), certificate: form.certificate || null,
                   genres: genres.length ? genres : null, plot: form.plot, excluded: form.excluded };
    if (item.kind === 'movie') body.programme_type = form.programme_type || null;
    if (item.kind === 'music') { body.artist = form.artist; body.concert = form.concert ? 1 : 0; }
    if (item.kind === 'advert') body.family_safe = form.family_safe ? 1 : 0;
    if (item.kind === 'movie' || item.kind === 'ident') body.home_channel_id = form.home_channel_id === '' ? null : Number(form.home_channel_id);
    const r = await tryApi(put(`/api/media/${id}`, body), { success: 'Saved' });
    if (r) { item = r; onsaved?.(); }
  });
  const clearOverrides = guard(async () => {
    const r = await tryApi(put(`/api/media/${id}`, { title: null, year: null, certificate: null, genres: null, plot: null, artist: null, programme_type: null }), { success: 'Overrides cleared' });
    if (r) { await load(); onsaved?.(); }
  });
  let overridden = $derived(item ? Object.keys(item.overrides ?? {}) : []);
</script>

<Drawer open={true} title={item?.title ?? 'Item'} subtitle={item?.filename ?? ''} {onclose}>
  {#if form}
    <div class="stack">
      <p class="scope" style="margin:0"><AppBadge app="pitv" /> PiTV's catalogue entry: these overrides win over pitv_content's index.</p>
      <div class="row">
        <span class="badge">{item.kind}</span>{#if item.kind === 'music' && item.concert}<span class="badge info">concert</span>{/if}{#if item.kind === 'advert' && item.family_safe === 0}<span class="badge warn">not family-safe</span>{/if}
        <span class="mono small">{item.vcodec ?? '?'}{item.acodec ? `/${item.acodec}` : ''}</span>
        {#if item.width}<span class="small muted">{item.width}×{item.height}{item.interlaced ? 'i' : ''}</span>{/if}
        {#if item.hwdec}<span class="badge ok">Hardware decode</span>{:else}<span class="badge warn" title="pitv_content transcodes this file to an H.264 copy when it is scheduled">Software decode</span>{/if}
        {#if item.transcoded_path}<span class="badge info">transcoded copy</span>{/if}
      </div>
      {#if !item.hwdec && (item.kind === 'movie' || item.kind === 'episode' || item.kind === 'music')}
        <p class="tiny muted">The Pi cannot hardware-decode this file; pitv_content transcodes it into the cache when it is scheduled{item.transcoded_path ? ' (a copy already exists)' : ''}.</p>
      {/if}
      <dl class="kv small">
        <dt>Plays from</dt><dd><Availability {item} /> <span class="tiny muted">{item.cached ? 'the cache' : item.origin === 'online' ? 'fetched online; requested again when scheduled' : 'the NAS until pitv_content copies it before air'}</span></dd>
        {#if item.cache_path}<dt>Cache copy</dt><dd class="mono" style="word-break:break-all">{item.cache_path}</dd>{/if}
        {#if item.uid}<dt>Index uid</dt><dd class="mono tiny" style="word-break:break-all">{item.uid}</dd>{/if}
        <dt>Duration</dt><dd>{fmtDuration(item.duration)}</dd>
        <dt>Size</dt><dd>{fmtBytes(item.size)}</dd>
        <dt>Path</dt><dd class="mono" style="word-break:break-all">{item.path}</dd>
        {#if item.attention}<dt>Attention</dt><dd><span class="badge warn">{item.attention}</span></dd>{/if}
      </dl>
      {#if overridden.length}
        <div class="row small muted">Overriding the index: {overridden.join(', ')} <button class="small ghost" onclick={clearOverrides} disabled={clearOverrides.busy}>Clear overrides</button></div>
      {/if}
      <div class="form-grid">
        {#if item.kind === 'music'}
          <label class="field">Artist<input bind:value={form.artist} /><span class="help">Indexed: {item.indexed?.artist ?? 'none'}</span></label>
        {/if}
        <label class="field">Title<input bind:value={form.title} /><span class="help">Indexed: {item.indexed?.title}</span></label>
        <label class="field">Year<input type="number" bind:value={form.year} min="1900" max="2100" /><span class="help">Indexed: {item.indexed?.year ?? 'none'}</span></label>
        <label class="field">Certificate
          <select bind:value={form.certificate}><option value="">(none)</option>{#each CERTIFICATES as c (c)}<option value={c}>{c}</option>{/each}</select>
          <span class="help">Indexed: {item.indexed?.certificate ?? 'none'}</span>
        </label>
        <label class="field">Genres<input bind:value={form.genres} placeholder="Comedy, Drama" /></label>
        {#if item.kind === 'movie'}
        <label class="field">What it is
          <select bind:value={form.programme_type}><option value="">{programmeTypeLabel(item.programme_type)} (read from its genres)</option>{#each PROGRAMME_TYPES as [v, l] (v)}<option value={v}>{l}</option>{/each}</select>
          <span class="help">Decides which channel theme it belongs to; genres only describe it. Changing this moves it to the right channel.</span>
        </label>
        {/if}
        {#if item.kind === 'movie' || item.kind === 'ident'}
          <label class="field">{item.kind === 'ident' ? 'Channel' : 'Home channel'}
            <select bind:value={form.home_channel_id}><option value="">(unassigned)</option>{#each channels as c (c.id)}<option value={c.id}>{c.number} {c.name}</option>{/each}</select>
            <span class="help">{item.kind === 'ident'
              ? `Which channel this ident introduces. Unassigned idents may air on any channel.${item.channel_hint ? ` Made for channel ${item.channel_hint}.` : ''}`
              : "Changing this moves the film's line-up entry to that channel; a film is on one channel only."}</span>
          </label>
        {/if}
        {#if item.kind === 'movie' || item.kind === 'episode'}
          <label class="field wide">Plot<textarea bind:value={form.plot}></textarea></label>
        {/if}
        {#if item.kind === 'music'}
          <label class="check"><input type="checkbox" bind:checked={form.concert} /> Concert<span class="help">Concerts fill the evening concert blocks instead of the video rotation.</span></label>
        {/if}
        {#if item.kind === 'advert'}
          <label class="check"><input type="checkbox" bind:checked={form.family_safe} /> Family-safe<span class="help">Off = never airs on a channel with family-safe adverts on (alcohol, tobacco, adult, gambling).</span></label>
        {/if}
        <label class="check"><input type="checkbox" bind:checked={form.excluded} /> Excluded from scheduling</label>
      </div>
      {#if item.upcoming?.length}
        <h3>Upcoming</h3>
        <ul class="plain small">{#each item.upcoming as u (u.id)}<li>{fmtDateTime(u.start_ts)}{u.locked ? ' 🔒' : ''}{u.replay ? ' (replay)' : ''}</li>{/each}</ul>
      {/if}
      {#if item.history?.length}
        <h3>Recently aired</h3>
        <ul class="plain small">{#each item.history as h (h.id)}<li>{fmtDateTime(h.started_at)}</li>{/each}</ul>
      {/if}
    </div>
  {:else}
    <div class="skeleton" style="height:200px"></div>
  {/if}
  {#snippet footer()}
    <button onclick={onclose}>Close</button>
    <button class="primary" onclick={save} disabled={save.busy || !form}>Save</button>
  {/snippet}
</Drawer>
