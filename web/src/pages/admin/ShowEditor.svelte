<script>
  import { untrack } from 'svelte';
  import { get, put, post, del, tryApi } from '../../lib/api.js';
  import { fmtDuration, fmtDateTime, fmtEpisode, WEEKDAYS, CERTIFICATES } from '../../lib/format.js';
  import { num } from '../../lib/util.js';
  import { guard } from '../../lib/guard.svelte.js';
  import Drawer from '../../components/Drawer.svelte';

  let { id, channels = [], onclose, onsaved } = $props();
  let show = $state(null);
  let form = $state(null);
  let cursorForm = $state({ season: 1, episode: 1 });

  async function load() {
    const s = await tryApi(get(`/api/shows/${id}`));
    if (!s) { onclose?.(); return; }
    show = s;
    form = {
      title: s.title ?? '', year: s.year ?? '', certificate: s.certificate ?? '', genres: (s.genres ?? []).join(', '),
      plot: s.plot ?? '', kids: !!s.kids, category: s.category || 'general', home_channel_id: s.home_channel_id ?? '', mode: s.mode ?? 'auto',
      anchor_time: s.anchor_time ?? '', anchor_days: new Set(s.anchor_days ?? []), rest_weeks: s.rest_weeks ?? 4, excluded: !!s.excluded,
    };
    if (s.cursor) cursorForm = { season: s.cursor.next_season, episode: s.cursor.next_episode };
  }
  $effect(() => { id; untrack(load); });

  function body() {
    const genres = form.genres.split(',').map((g) => g.trim()).filter(Boolean);
    return {
      title: form.title, year: num(form.year, { min: 1900, max: 2100, int: true }), certificate: form.certificate || null,
      genres: genres.length ? genres : null, plot: form.plot, kids: form.kids ? 1 : 0,
      category: form.category, home_channel_id: form.home_channel_id === '' ? null : Number(form.home_channel_id), mode: form.mode,
      anchor_time: form.mode === 'auto' ? null : form.anchor_time || null,
      anchor_days: form.mode === 'auto' ? null : [...form.anchor_days].sort(),
      rest_weeks: num(form.rest_weeks, { min: 0, max: 52, int: true, fallback: 0 }), excluded: form.excluded,
    };
  }
  const save = guard(async () => {
    const r = await tryApi(put(`/api/shows/${id}`, body()), { success: 'Show saved' });
    if (r) { show = r; onsaved?.(); }
  });
  const clearOverrides = guard(async () => {
    const r = await tryApi(put(`/api/shows/${id}`, { title: null, year: null, certificate: null, genres: null, plot: null, kids: null }), { success: 'Overrides cleared' });
    if (r) { await load(); onsaved?.(); }
  });
  const setCursor = guard(async (season, episode) => {
    season = num(season, { min: 0, int: true, fallback: 0 });
    episode = num(episode, { min: 0, int: true, fallback: 0 });
    if (await tryApi(post(`/api/shows/${id}/cursor`, { season, episode }), { success: `Next episode set to S${season}E${episode}` })) load();
  });
  const clearCursor = guard(async () => {
    if (await tryApi(del(`/api/shows/${id}/cursor`), { success: 'Cursor cleared' })) load();
  });
  function toggleDay(d) {
    if (form.anchor_days.has(d)) form.anchor_days.delete(d); else form.anchor_days.add(d);
    form.anchor_days = new Set(form.anchor_days);
  }
  let overridden = $derived(show ? Object.keys(show.overrides ?? {}) : []);
</script>

<Drawer open={true} title={show?.title ?? 'Show'} subtitle={show?.folder ?? ''} {onclose} wide>
  {#if form}
    <div class="stack">
      {#if overridden.length}
        <div class="row small muted">Overriding scanned: {overridden.join(', ')} <button class="small ghost" onclick={clearOverrides} disabled={clearOverrides.busy}>Clear overrides</button></div>
      {/if}
      <div class="form-grid">
        <label class="field">Title<input bind:value={form.title} /><span class="help">Scanned: {show.scanned.title}</span></label>
        <label class="field">Year<input type="number" bind:value={form.year} min="1900" max="2100" /><span class="help">Scanned: {show.scanned.year ?? 'none'}</span></label>
        <label class="field">Certificate
          <select bind:value={form.certificate}><option value="">(none)</option>{#each CERTIFICATES as c (c)}<option value={c}>{c}</option>{/each}</select>
          <span class="help">Scanned: {show.scanned.certificate ?? 'none'}</span>
        </label>
        <label class="field">Genres<input bind:value={form.genres} placeholder="Comedy, Drama" /><span class="help">Comma separated</span></label>
        <label class="field wide">Plot<textarea bind:value={form.plot}></textarea></label>
        <label class="check"><input type="checkbox" bind:checked={form.kids} /> Children's programme</label>
        <label class="check"><input type="checkbox" bind:checked={form.excluded} /> Excluded from scheduling</label>
      </div>
      <hr />
      <div class="form-grid">
        <label class="field">Category
          <select bind:value={form.category}><option value="general">General</option><option value="sport">Sport</option><option value="kids">Children's</option><option value="cartoon">Cartoon</option></select>
          <span class="help">Sport gets weekend afternoon and midweek late slots; cartoons are routed to a cartoons channel (set automatically from the genres).</span>
        </label>
        <label class="field">Home channel
          <select bind:value={form.home_channel_id}><option value="">(unassigned)</option>{#each channels as c (c.id)}<option value={c.id}>{c.number} {c.name}</option>{/each}</select>
          <span class="help">Changing this moves the show's line-up entry to that channel; a show is on one channel only.</span>
        </label>
        <label class="field">Scheduling mode
          <select bind:value={form.mode}><option value="auto">Auto</option><option value="strip">Strip (same time, chosen days)</option><option value="weekly">Weekly (one day a week)</option></select>
        </label>
        {#if form.mode !== 'auto'}
          <label class="field">Anchor time<input type="time" bind:value={form.anchor_time} /><span class="help">Start time the episodes are pinned to.</span></label>
          <div class="field"><span>Days</span>
            <div class="row">{#each WEEKDAYS as d, i (d)}<label class="check small"><input type="checkbox" checked={form.anchor_days.has(i)} onchange={() => toggleDay(i)} />{d}</label>{/each}</div>
          </div>
        {/if}
        <label class="field">Rest weeks<input type="number" bind:value={form.rest_weeks} min="0" max="52" /><span class="help">Weeks off air after the last episode before the series starts again.</span></label>
      </div>
      <hr />
      <h3>Next episode</h3>
      <div class="row small">
        {#if show.cursor}
          <span>Cursor: S{show.cursor.next_season}E{show.cursor.next_episode}</span>
          <button class="small ghost" onclick={clearCursor} disabled={clearCursor.busy}>Clear</button>
        {:else}
          <span class="muted">No cursor set{show.last_aired ? ` (last aired S${show.last_aired.season}E${show.last_aired.episode})` : ''}.</span>
        {/if}
        <span class="inline-form">
          <input class="xnarrow" type="number" min="0" bind:value={cursorForm.season} aria-label="Season" />
          <input class="xnarrow" type="number" min="0" bind:value={cursorForm.episode} aria-label="Episode" />
          <button class="small" onclick={() => setCursor(cursorForm.season, cursorForm.episode)} disabled={setCursor.busy}>Set next</button>
        </span>
      </div>
      {#if show.upcoming?.length}
        <h3>Upcoming airings</h3>
        <ul class="plain small">
          {#each show.upcoming as u (u.id)}
            <li>{fmtDateTime(u.start_ts)} · Ch {channels.find((c) => c.id === u.channel_id)?.number ?? u.channel_id} · {u.subtitle}{u.locked ? ' 🔒' : ''}</li>
          {/each}
        </ul>
      {/if}
      <h3>Episodes ({show.episodes.length})</h3>
      <div class="table-wrap">
        <table>
          <thead><tr><th>Ep</th><th>Title</th><th>Length</th><th>Codec</th><th></th></tr></thead>
          <tbody>
            {#each show.episodes as e (e.id)}
              <tr class:dim={e.missing || e.excluded}>
                <td class="nowrap mono small">{fmtEpisode(e.season, e.episode)}</td>
                <td>{e.title}{#if e.attention}<span class="badge warn" title={e.attention}>!</span>{/if}{#if e.missing}<span class="badge danger">missing</span>{/if}</td>
                <td class="small">{fmtDuration(e.duration)}</td>
                <td class="small"><span class="mono">{e.vcodec ?? '?'}</span> {#if e.hwdec}<span class="badge ok">HW</span>{:else}<span class="badge warn">SW</span>{/if}</td>
                <td class="right"><button class="small ghost" onclick={() => setCursor(e.season ?? 0, e.episode ?? 0)}>Next</button></td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {:else}
    <div class="skeleton" style="height:200px"></div>
  {/if}
  {#snippet footer()}
    <button onclick={onclose}>Close</button>
    <button class="primary" onclick={save} disabled={save.busy || !form}>Save</button>
  {/snippet}
</Drawer>

<style>
  .dim td { opacity: .55; }
</style>
