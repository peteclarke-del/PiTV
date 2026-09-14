<script>
  import { get, put, tryApi } from '../../lib/api.js';
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
             plot: m.plot ?? '', excluded: !!m.excluded, channel_hint: m.channel_hint ?? '' };
  }
  $effect(() => { id; load(); }); // eslint-disable-line no-unused-expressions

  async function save() {
    saving = true;
    const genres = form.genres.split(',').map((g) => g.trim()).filter(Boolean);
    const body = { title: form.title, year: form.year === '' ? null : Number(form.year), certificate: form.certificate || null,
                   genres: genres.length ? genres : null, plot: form.plot, excluded: form.excluded };
    if (item.kind === 'ident') body.channel_hint = form.channel_hint === '' ? null : Number(form.channel_hint);
    const r = await tryApi(put(`/api/media/${id}`, body), { success: 'Saved' });
    saving = false;
    if (r) { item = r; onsaved?.(); }
  }
  async function clearOverrides() {
    const r = await tryApi(put(`/api/media/${id}`, { title: null, year: null, certificate: null, genres: null, plot: null }), { success: 'Overrides cleared' });
    if (r) { await load(); onsaved?.(); }
  }
  let overridden = $derived(item ? Object.keys(item.overrides ?? {}) : []);
</script>

<Drawer open={true} title={item?.title ?? 'Item'} subtitle={item?.filename ?? ''} {onclose}>
  {#if form}
    <div class="stack">
      <div class="row">
        <span class="badge">{item.kind}</span>
        <span class="mono small">{item.vcodec ?? '?'}{item.acodec ? `/${item.acodec}` : ''}</span>
        {#if item.width}<span class="small muted">{item.width}×{item.height}{item.interlaced ? 'i' : ''}</span>{/if}
        {#if item.hwdec}<span class="badge ok">Hardware decode</span>{:else}<span class="badge warn">Software decode</span>{/if}
        {#if item.transcoded_path}<span class="badge info">transcoded copy</span>{/if}
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
