<script>
  import { get, put, post, tryApi } from '../../lib/api.js';
  import { toast } from '../../lib/stores.svelte.js';
  import { fmtDuration, fmtBytes, fmtDateTime, CERTIFICATES } from '../../lib/format.js';
  import Drawer from '../../components/Drawer.svelte';

  let { id, onclose, onsaved } = $props();
  let item = $state(null);
  let form = $state(null);
  let saving = $state(false);

  async function load() {
    const m = await tryApi(get(`/api/media/${id}`));
    if (!m) { onclose?.(); return; }
    item = m;
    form = { title: m.title ?? '', year: m.year ?? '', certificate: m.certificate ?? '', genres: (m.genres ?? []).join(', '),
             plot: m.plot ?? '', excluded: !!m.excluded, channel_hint: m.channel_hint ?? '', artist: m.artist ?? '', concert: !!m.concert, family_safe: m.family_safe !== 0 };
  }
  $effect(() => { id; load(); }); // eslint-disable-line no-unused-expressions

  async function save() {
    saving = true;
    const genres = form.genres.split(',').map((g) => g.trim()).filter(Boolean);
    const body = { title: form.title, year: form.year === '' ? null : Number(form.year), certificate: form.certificate || null,
                   genres: genres.length ? genres : null, plot: form.plot, excluded: form.excluded };
    if (item.kind === 'ident') body.channel_hint = form.channel_hint === '' ? null : Number(form.channel_hint);
    if (item.kind === 'music') { body.artist = form.artist; body.concert = form.concert ? 1 : 0; }
    if (item.kind === 'advert') body.family_safe = form.family_safe ? 1 : 0;
    const r = await tryApi(put(`/api/media/${id}`, body), { success: 'Saved' });
    saving = false;
    if (r) { item = r; onsaved?.(); }
  }
  async function clearOverrides() {
    const r = await tryApi(put(`/api/media/${id}`, { title: null, year: null, certificate: null, genres: null, plot: null, artist: null }), { success: 'Overrides cleared' });
    if (r) { await load(); onsaved?.(); }
  }
  let overridden = $derived(item ? Object.keys(item.overrides ?? {}) : []);
  let queued = $state(false);
  async function transcode() {
    const r = await tryApi(post('/api/transcode', { media_id: id }));
    if (r) { queued = true; toast.success(r.added ? 'Queued for transcoding' : 'Already in the transcode queue'); }
  }
</script>

<Drawer open={true} title={item?.title ?? 'Item'} subtitle={item?.filename ?? ''} {onclose}>
  {#if form}
    <div class="stack">
      <div class="row">
        <span class="badge">{item.kind}</span>{#if item.kind === 'music' && item.concert}<span class="badge info">concert</span>{/if}{#if item.kind === 'advert' && item.family_safe === 0}<span class="badge warn">not family-safe</span>{/if}
        <span class="mono small">{item.vcodec ?? '?'}{item.acodec ? `/${item.acodec}` : ''}</span>
        {#if item.width}<span class="small muted">{item.width}×{item.height}{item.interlaced ? 'i' : ''}</span>{/if}
        {#if item.hwdec}<span class="badge ok">Hardware decode</span>{:else}<span class="badge warn">Software decode</span>{/if}
        {#if item.transcoded_path}<span class="badge info">transcoded copy</span>{/if}
        {#if !item.hwdec && !item.transcoded_path && (item.kind === 'movie' || item.kind === 'episode')}
          <button class="small" onclick={transcode} disabled={queued}>{queued ? 'Queued' : 'Transcode'}</button>
        {/if}
      </div>
      <dl class="kv small">
        <dt>Duration</dt><dd>{fmtDuration(item.duration)}</dd>
        <dt>Size</dt><dd>{fmtBytes(item.size)}</dd>
        <dt>Path</dt><dd class="mono" style="word-break:break-all">{item.path}</dd>
        {#if item.attention}<dt>Attention</dt><dd><span class="badge warn">{item.attention}</span></dd>{/if}
      </dl>
      {#if overridden.length}
        <div class="row small muted">Overriding scanned: {overridden.join(', ')} <button class="small ghost" onclick={clearOverrides}>Clear overrides</button></div>
      {/if}
      <div class="form-grid">
        {#if item.kind === 'music'}
          <label class="field">Artist<input bind:value={form.artist} /><span class="help">Scanned: {item.scanned.artist ?? 'none'}</span></label>
        {/if}
        <label class="field">Title<input bind:value={form.title} /><span class="help">Scanned: {item.scanned.title}</span></label>
        <label class="field">Year<input type="number" bind:value={form.year} min="1900" max="2100" /><span class="help">Scanned: {item.scanned.year ?? 'none'}</span></label>
        <label class="field">Certificate
          <select bind:value={form.certificate}><option value="">(none)</option>{#each CERTIFICATES as c (c)}<option value={c}>{c}</option>{/each}</select>
          <span class="help">Scanned: {item.scanned.certificate ?? 'none'}</span>
        </label>
        <label class="field">Genres<input bind:value={form.genres} placeholder="Comedy, Drama" /></label>
        {#if item.kind === 'ident'}
          <label class="field">Channel number<input type="number" class="narrow" bind:value={form.channel_hint} min="1" /><span class="help">Which channel this ident belongs to.</span></label>
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
        <ul class="small" style="margin:0;padding-left:1.1rem">{#each item.upcoming as u (u.id)}<li>{fmtDateTime(u.start_ts)}{u.locked ? ' 🔒' : ''}{u.replay ? ' (replay)' : ''}</li>{/each}</ul>
      {/if}
      {#if item.history?.length}
        <h3>Recently aired</h3>
        <ul class="small" style="margin:0;padding-left:1.1rem">{#each item.history as h (h.id)}<li>{fmtDateTime(h.started_at)}</li>{/each}</ul>
      {/if}
    </div>
  {:else}
    <div class="skeleton" style="height:200px"></div>
  {/if}
  {#snippet footer()}
    <button onclick={onclose}>Close</button>
    <button class="primary" onclick={save} disabled={saving || !form}>Save</button>
  {/snippet}
</Drawer>
