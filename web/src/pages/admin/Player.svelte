<script>
  import { untrack } from 'svelte';
  import { get, post, tryApi, confirmApi } from '../../lib/api.js';
  import { changes, player, clock } from '../../lib/stores.svelte.js';
  import { fmtDateTime, fmtDuration, fmtRange, fmtBytes, fmtAgo } from '../../lib/format.js';
  import Remote from '../../components/Remote.svelte';
  import PlayerStatus from '../../components/PlayerStatus.svelte';
  import ProgressBar from '../../components/ProgressBar.svelte';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
  import AppBadge from '../../components/AppBadge.svelte';
  import { playbackIssue } from '../../lib/playback.js';
  import KeymapEditor from './KeymapEditor.svelte';

  let channels = $state([]);
  let history = $state([]);
  let s = $derived(player.state);
  async function load() {
    const r = await tryApi(Promise.all([get('/api/channels'), get('/api/history', { limit: 50 })]));
    if (r) [channels, history] = r;
  }
  $effect(() => { changes.library; untrack(load); });
  // The airing history grows whenever the player moves to a new slot.
  let lastSlot = null;
  $effect(() => {
    const id = s.slot?.id ?? null;
    if (id !== lastSlot) { lastSlot = id; untrack(load); }
  });

  const restart = () => confirmApi('Restart the player service? The picture will drop for a few seconds.', { title: 'Restart player', okLabel: 'Restart' },
    () => post('/api/system/service/pitv-player.service/restart'), { success: 'Restart requested' });
  let slotProgress = $derived.by(() => {
    const sl = s.slot; if (!sl || !sl.start_ts || !sl.end_ts) return 0;
    return (clock.ts - sl.start_ts) / (sl.end_ts - sl.start_ts);
  });
  let cache = $derived(s.cache ?? { enabled: false });
  let cacheFrac = $derived(cache.enabled && cache.max ? cache.used / cache.max : 0);
  let maint = $derived(s.maintenance ?? {});
</script>

<div class="stack">
  <div class="two">
    <div class="stack">
      <div class="card">
        <div class="card-title"><h3>Player</h3><AppBadge app="pitv" /><button class="small danger" onclick={restart}>Restart player</button></div>
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
          {#if s.error}{@const issue = playbackIssue(s.error)}
            <p class="mt {issue.cls === 'warn' ? 'warn-box' : 'err-box'}"><span class="badge {issue.cls}">{issue.label}</span> {s.error}
              {#if issue.label === 'NAS fallback'}<span class="tiny muted" style="display:block">Playing the NAS original because the cache copy is missing; pitv_content should deliver it before the next airing.</span>
              {:else if issue.label === 'Technical difficulties'}<span class="tiny muted" style="display:block">Neither the cache copy nor the NAS original could be played, so the technical difficulties card is on air.</span>{/if}</p>
          {/if}
          <dl class="kv small mt">
            <dt>Input devices</dt><dd>{s.input_devices?.length ? s.input_devices.join(', ') : 'none detected'}</dd>
            {#if s.last_key}<dt>Last key</dt><dd><code>{s.last_key.key}</code> maps to {s.last_key.action ?? 'unmapped'} <span class="muted">({fmtAgo(s.last_key.ts, clock.ts)})</span></dd>{/if}
            <dt>State time</dt><dd>{fmtDateTime(s.ts)}</dd>
          </dl>
        {:else}
          <p class="muted small mt">{s.error || 'The player daemon is not connected. Start it with the pitv-player service.'}</p>
        {/if}
      </div>

      <div class="card">
        <div class="card-title"><h3>Stream</h3><AppBadge app="pitv" /></div>
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
        <div class="card-title"><h3>Cache</h3><AppBadge app="pitv" /></div>
        <p class="scope">pitv_content fills the cache; PiTV plays from it and evicts under the size cap.</p>
        {#if !s.online}<p class="muted small">Unknown while the player is offline.</p>
        {:else if !cache.enabled}<p class="muted small">Local cache disabled. Set a cache directory under Weighting, Cache.</p>
        {:else}
          <ProgressBar value={cacheFrac} />
          <p class="small muted" style="margin:.4rem 0 0">{fmtBytes(cache.used)} of {fmtBytes(cache.max)} used · {cache.files} files{cache.free != null ? ` · ${fmtBytes(cache.free)} free on disk` : ''}</p>
          <p class="tiny muted mono" style="margin:0">{cache.dir}</p>
          {#if cache.tool_running}<div class="mt"><span class="badge info">pitv_content running</span> <span class="tiny muted">filling the cache now</span></div>{/if}
        {/if}
      </div>

      <div class="card">
        <div class="card-title"><h3>Maintenance</h3><AppBadge app="pitv" /></div>
        {#if !s.online}<p class="muted small">Unknown while the player is offline.</p>
        {:else}
          <dl class="kv small">
            <dt>Last build</dt><dd>{#if maint.last_build}<span class="badge {maint.last_build.status === 'ok' ? 'ok' : 'warn'}">{maint.last_build.status}</span> {maint.last_build.summary} <span class="muted">({fmtAgo(maint.last_build.at, clock.ts)})</span>{:else}<span class="muted">not yet</span>{/if}</dd>
            <dt>Last import</dt><dd>{#if maint.last_import}<span class="badge {maint.last_import.status === 'ok' ? 'ok' : 'warn'}">{maint.last_import.status}</span> {maint.last_import.summary} <span class="muted">({fmtAgo(maint.last_import.at, clock.ts)})</span>{:else}<span class="muted">not yet</span>{/if}</dd>
            <dt>Readiness</dt><dd>{#if maint.last_readiness}<span class="badge {maint.last_readiness.status === 'ok' ? 'ok' : 'warn'}">{maint.last_readiness.status}</span> {maint.last_readiness.summary} <span class="muted">({fmtAgo(maint.last_readiness.at, clock.ts)})</span>{:else}<span class="muted">not checked yet</span>{/if}</dd>
            {#if maint.error}<dt>Error</dt><dd><span class="badge danger">{maint.error}</span></dd>{/if}
            <dt>Wanted</dt><dd><a class="small" href="#/admin/wanted">Open the wanted list</a> · fetched by pitv_content</dd>
          </dl>
        {/if}
      </div>
    </div>

    <div class="stack">
      <div class="card"><div class="card-title"><h3>Remote</h3><AppBadge app="pitv" /></div><Remote {channels} /></div>
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

  <div class="card"><div class="card-title"><h3>Remote keymap</h3><AppBadge app="pitv" /></div><KeymapEditor /></div>
</div>

<style>
  .two { display: grid; gap: 1rem; grid-template-columns: 1fr; }
  @media (min-width: 900px) { .two { grid-template-columns: 1fr 1fr; align-items: start; } }
  .nowplaying { display: flex; flex-direction: column; gap: .25rem; }
  .title { font-size: 1.15rem; font-weight: 650; }
</style>
