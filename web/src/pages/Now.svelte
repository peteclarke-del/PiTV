<script>
  import { untrack } from 'svelte';
  import { get, normalisePlayer } from '../lib/api.js';
  import { tuneChannel } from '../lib/actions.js';
  import { navigate } from '../lib/router.js';
  import { changes, clock, player, toast } from '../lib/stores.svelte.js';
  import { fmtRange, fmtTime, fmtDuration, plural, safeColour } from '../lib/format.js';
  import ChannelBadge from '../components/ChannelBadge.svelte';
  import ProgressBar from '../components/ProgressBar.svelte';
  import PlayerStatus from '../components/PlayerStatus.svelte';

  let data = $state(null);
  let error = $state('');
  let loading = $state(false);
  let lastLoad = 0;

  async function load() {
    loading = true;
    try {
      const d = await get('/api/now', { next: 3 });
      data = d;
      if (d.player && !player.connected) player.state = normalisePlayer(d.player);
      error = '';
    } catch (e) {
      // Shown inline until the first load succeeds; after that a toast, once per outage, so a
      // refetch every few seconds does not stack them up.
      if (data && !error) toast.error(e.detail || e.message);
      error = e.detail || e.message;
    }
    loading = false;
    lastLoad = Date.now();
  }

  $effect(() => {
    changes.schedule;
    untrack(load);
  });

  // When the current slot on any channel ends, refetch (at most once every few seconds).
  $effect(() => {
    const ts = clock.ts;
    const d = untrack(() => data);
    if (!d || untrack(() => loading)) return;
    const due = d.channels.some((c) => c.now && c.now.end_ts <= ts) ||
      d.channels.some((c) => !c.now && c.next[0] && c.next[0].start_ts <= ts);
    if (due && Date.now() - lastLoad > 3000) untrack(load);
  });

  function progress(slot) {
    const len = slot.end_ts - slot.start_ts;
    return len > 0 ? (clock.ts - slot.start_ts) / len : 0;
  }

</script>

<div class="page">
  <div class="page-head">
    <h1>Now &amp; Next</h1>
    <PlayerStatus />
  </div>

  {#if error && !data}
    <div class="empty">{error}</div>
  {:else if !data}
    <div class="grid">{#each [1, 2, 3, 4] as i (i)}<div class="card skeleton" style="height:180px"></div>{/each}</div>
  {:else if !data.channels.length}
    <div class="empty">No channels are enabled. Set some up in <a href="#/admin/channels">Admin, Channels</a>.</div>
  {:else}
    <div class="grid">
      {#each data.channels as c (c.channel.id)}
        {@const s = c.now}
        <article class="card ch" style="--c:{safeColour(c.channel.colour)}">
          <div class="chhead">
            <ChannelBadge channel={c.channel} size="lg" />
            <button class="primary small" onclick={() => tuneChannel(c.channel.number)}
                    disabled={!player.state.online} title={player.state.online ? 'Tune the TV to this channel' : 'Player offline'}>On the TV</button>
            <button class="small" onclick={() => navigate(`/channel/${c.channel.number}`)} title="Watch this channel in this browser">In the browser</button>
          </div>
          {#if s}
            {#if s.break}
              <div class="now">
                <div class="label">Ad break</div>
                {#if s.previous_programme}
                  <div class="muted small">after <b>{s.previous_programme.title}</b></div>
                {/if}
                <div class="muted small">{fmtRange(s.start_ts, s.end_ts)}</div>
                <ProgressBar value={progress(s)} />
              </div>
            {:else}
              <div class="now">
                <div class="label">Now</div>
                <div class="title">{s.title}</div>
                {#if s.block}
                  {#if s.video_title && s.video_title !== s.title}<div class="video">{s.video_title}</div>{/if}
                  <div class="muted small">{s.subtitle || s.block}{s.items > 1 ? ` · ${plural(s.items, 'programme')}` : ''}</div>
                {:else if s.subtitle}<div class="muted small">{s.subtitle}</div>{/if}
                <div class="muted small">{fmtRange(s.start_ts, s.end_ts)} · {fmtDuration(s.end_ts - s.start_ts)}{s.replay ? ' · replay' : ''}</div>
                <ProgressBar value={progress(s)} />
              </div>
            {/if}
          {:else}
            <div class="now"><div class="label">Now</div><div class="muted">Nothing scheduled{c.next[0] ? ` until ${fmtTime(c.next[0].start_ts)}` : ''}</div></div>
          {/if}
          {#if c.next.length}
            <ul class="next">
              {#each c.next as n (n.id)}
                <li><span class="mono time">{fmtTime(n.start_ts)}</span><span class="truncate"><b>{n.title}</b>{n.block && (n.items ?? 1) > 1 ? ` · ${plural(n.items, 'programme')}` : n.subtitle ? ` · ${n.subtitle}` : ''}</span></li>
              {/each}
            </ul>
          {/if}
        </article>
      {/each}
    </div>
  {/if}
</div>

<style>
  .ch { border-top: 4px solid var(--c); display: flex; flex-direction: column; gap: .75rem; }
  .chhead { display: flex; align-items: center; justify-content: space-between; gap: .5rem; }
  .now { display: flex; flex-direction: column; gap: .2rem; }
  .label { font-size: .72rem; text-transform: uppercase; letter-spacing: .08em; font-weight: 700; color: var(--accent); }
  .title { font-size: 1.15rem; font-weight: 650; line-height: 1.25; }
  .video { font-size: .95rem; color: var(--info); font-weight: 550; }
  .next { list-style: none; margin: 0; padding: .5rem 0 0; border-top: 1px dashed var(--border); display: flex; flex-direction: column; gap: .3rem; font-size: .9rem; }
  .next li { display: flex; gap: .6rem; align-items: baseline; min-width: 0; }
  .time { color: var(--fg-muted); flex: none; font-size: .85rem; }
</style>
