<script>
  import { untrack } from 'svelte';
  import { get, post, tryApi } from '../../lib/api.js';
  import { changes, confirm, player, clock } from '../../lib/stores.svelte.js';
  import { fmtDateTime, fmtDuration, fmtRange, fmtBytes, fmtAgo } from '../../lib/format.js';
  import Remote from '../../components/Remote.svelte';
  import PlayerStatus from '../../components/PlayerStatus.svelte';
  import ProgressBar from '../../components/ProgressBar.svelte';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
  import KeymapEditor from './KeymapEditor.svelte';

  let channels = $state([]);
  let history = $state([]);
  let s = $derived(player.state);
  async function load() {
    try {
      const [c, h] = await Promise.all([get('/api/channels'), get('/api/history', { limit: 50 })]);
      channels = c; history = h;
    } catch { /* ignore */ }
  }
  $effect(() => { changes.library; untrack(load); }); // eslint-disable-line no-unused-expressions
  // Refresh history when the slot changes.
  let lastSlot = null;
  $effect(() => {
    const id = s.slot?.id ?? null;
    if (id !== lastSlot) { lastSlot = id; untrack(load); }
  });

  async function restart() {
    if (await confirm('Restart the player service? The picture will drop for a few seconds.', { title: 'Restart player', okLabel: 'Restart' }))
      tryApi(post('/api/system/service/pitv-player/restart'), { success: 'Restart requested' });
  }
  let slotProgress = $derived.by(() => {
    const sl = s.slot; if (!sl || !sl.start_ts || !sl.end_ts) return 0;
    return (clock.ts - sl.start_ts) / (sl.end_ts - sl.start_ts);
  });
  let cache = $derived(s.cache ?? { enabled: false });
  let cacheFrac = $derived(cache.enabled && cache.max ? cache.used / cache.max : 0);
  let acq = $derived(s.acquire?.current ?? null);
  let maint = $derived(s.maintenance ?? {});
</script>

<div class="stack">
  <div class="two">
    <div class="stack">
      <div class="card">
        <div class="card-title"><h3>Player</h3><button class="small danger" onclick={restart}>Restart player</button></div>
        <PlayerStatus />
        {#if s.online}
          <div class="nowplaying mt">
            {#if s.channel}<ChannelBadge channel={s.channel} />{/if}
            {#if s.slot}
              <div class="title">{s.slot.title}</div>
              {#if s.slot.subtitle}<div class="muted small">{s.slot.subtitle}</div>{/if}
              <div class="muted small">{fmtRange(s.slot.start_ts, s.slot.end_ts)} · {s.slot.kind}{s.position !== null ? ` · ${fmtDuration(s.position)} into file` : ''}</div>
              <ProgressBar value={slotProgress} />
            {:else if s.testcard}
              <div class="muted">Showing the test card.</div>
            {:else}
              <div class="muted">Nothing playing.</div>
            {/if}
          </div>
          <div class="row mt">
            {#if s.paused}<span class="badge warn">Paused</span>{/if}
            {#if s.behind_live}<span class="badge info">Behind live</span>{/if}
            {#if s.guide_open}<span class="badge">Guide open</span>{/if}
            {#if s.muted}<span class="badge">Muted</span>{/if}
            {#if s.volume !== null}<span class="badge">Vol {s.volume}</span>{/if}
            <span class="badge {s.hwdec ? 'ok' : 'warn'}">{s.hwdec ? `hwdec ${s.hwdec}` : 'software decode'}</span>
            {#if s.on_pi}<span class="badge">on Pi</span>{:else}<span class="badge">desktop</span>{/if}
          </div>
          {#if s.error}<p class="warn-box mt"><b>Player error:</b> {s.error}</p>{/if}
          <dl class="kv small mt">
            <dt>Input devices</dt><dd>{s.input_devices?.length ? s.input_devices.join(', ') : 'none detected'}</dd>
            {#if s.last_key}<dt>Last key</dt><dd><code>{s.last_key.key}</code> → {s.last_key.action ?? 'unmapped'} <span class="muted">({fmtAgo(s.last_key.ts, clock.ts)})</span></dd>{/if}
            <dt>State time</dt><dd>{fmtDateTime(s.ts)}</dd>
          </dl>
        {:else}
          <p class="muted small mt">{s.error || 'The player daemon is not connected. Start it with the pitv-player service.'}</p>
        {/if}
      </div>

      <div class="card">
        <div class="card-title"><h3>Stream</h3></div>
        {#if !s.online}<p class="muted small">Unknown while the player is offline.</p>
        {:else if !s.file && !s.stream}<p class="muted small">Nothing is playing.</p>
        {:else}
          {#if s.file}<div class="tiny muted mono" style="word-break:break-all">{s.file}</div>{/if}
          {#if s.stream && Object.keys(s.stream).length}
            <dl class="kv small mt">
              {#each Object.entries(s.stream) as [k, v] (k)}
                <dt>{k}</dt><dd>{#if k === 'hwdec'}<span class="badge {v ? 'ok' : 'warn'}">{v || 'software'}</span>{:else}{v === null || v === undefined || v === '' ? '–' : String(v)}{/if}</dd>
              {/each}
            </dl>
          {:else}<p class="muted small mt">Stream details not reported yet.</p>{/if}
        {/if}
      </div>

      <div class="card">
        <div class="card-title"><h3>Cache</h3></div>
        {#if !s.online}<p class="muted small">Unknown while the player is offline.</p>
        {:else if !cache.enabled}<p class="muted small">Local cache disabled. Set a cache directory under Weighting → Cache.</p>
        {:else}
          <ProgressBar value={cacheFrac} />
          <p class="small muted" style="margin:.4rem 0 0">{fmtBytes(cache.used)} of {fmtBytes(cache.max)} used · {cache.files} files{cache.free != null ? ` · ${fmtBytes(cache.free)} free on disk` : ''}</p>
          <p class="tiny muted mono" style="margin:0">{cache.dir}</p>
          {#if cache.copying}
            <div class="mt small">Copying <b>{cache.copying.name}</b></div>
            <ProgressBar value={cache.copying.size ? cache.copying.done / cache.copying.size : 0} />
            <div class="tiny muted">{fmtBytes(cache.copying.done)} of {fmtBytes(cache.copying.size)}</div>
          {/if}
        {/if}
      </div>

      <div class="card">
        <div class="card-title"><h3>Maintenance &amp; acquisition</h3></div>
        {#if !s.online}<p class="muted small">Unknown while the player is offline.</p>
        {:else}
          <dl class="kv small">
            <dt>Last build</dt><dd>{#if maint.last_build}<span class="badge {maint.last_build.status === 'ok' ? 'ok' : 'warn'}">{maint.last_build.status}</span> {maint.last_build.summary} <span class="muted">({fmtAgo(maint.last_build.at, clock.ts)})</span>{:else}<span class="muted">not yet</span>{/if}</dd>
            <dt>Last scan</dt><dd>{maint.last_scan ? fmtAgo(maint.last_scan, clock.ts) : 'not yet'}</dd>
            <dt>Readiness</dt><dd>{#if maint.last_readiness}<span class="badge {maint.last_readiness.status === 'ok' ? 'ok' : 'warn'}">{maint.last_readiness.status}</span> {maint.last_readiness.summary} <span class="muted">({fmtAgo(maint.last_readiness.at, clock.ts)})</span>{:else}<span class="muted">not checked yet</span>{/if}</dd>
            {#if maint.error}<dt>Error</dt><dd><span class="badge danger">{maint.error}</span></dd>{/if}
            <dt>Acquiring</dt><dd>
              {#if acq}
                <span class="badge info">{acq.kind}</span> {acq.title ?? `#${acq.id}`} · {acq.status}{acq.message ? ` · ${acq.message}` : ''}
                {#if acq.progress}<ProgressBar value={acq.progress > 1 ? acq.progress / 100 : acq.progress} />{/if}
              {:else}<span class="muted">idle</span>{/if}
              <a class="small" href="#/admin/acquire">Open queue</a>
            </dd>
          </dl>
        {/if}
      </div>
    </div>

    <div class="stack">
      <div class="card"><div class="card-title"><h3>Remote</h3></div><Remote {channels} /></div>
      <div class="card pad-0 table-wrap">
        <table>
          <thead><tr><th>Started</th><th>Ch</th><th>Title</th><th>Length</th></tr></thead>
          <tbody>
            {#each history as h (h.id)}
              <tr><td class="nowrap small">{fmtDateTime(h.started_at)}</td><td>{h.channel_number ?? h.channel_id}</td><td>{h.title}</td>
                <td class="small muted">{h.ended_at ? fmtDuration(h.ended_at - h.started_at) : 'playing'}</td></tr>
            {:else}
              <tr><td colspan="4" class="empty">Nothing has aired yet.</td></tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  </div>

  <div class="card"><div class="card-title"><h3>Remote keymap</h3></div><KeymapEditor /></div>
</div>

<style>
  .two { display: grid; gap: 1rem; grid-template-columns: 1fr; }
  @media (min-width: 900px) { .two { grid-template-columns: 1fr 1fr; align-items: start; } }
  .nowplaying { display: flex; flex-direction: column; gap: .25rem; }
  .title { font-size: 1.15rem; font-weight: 650; }
</style>
