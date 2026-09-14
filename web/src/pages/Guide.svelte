<script>
  import { untrack } from 'svelte';
  import { get, post, tryApi } from '../lib/api.js';
  import { changes, clock, route, player } from '../lib/stores.svelte.js';
  import { setQuery } from '../lib/router.js';
  import { fmtDay, fmtRange, fmtDuration } from '../lib/format.js';
  import EpgGrid from '../components/EpgGrid.svelte';
  import Drawer from '../components/Drawer.svelte';
  import ChannelBadge from '../components/ChannelBadge.svelte';

  let days = $state(null);          // /api/schedule/days
  let day = $state(route.query.day || '');
  let data = $state(null);          // /api/schedule
  let loading = $state(false);
  let selected = $state(null);
  let grid = $state(null);
  let ppm = $state(window.matchMedia('(max-width: 600px)').matches ? 3 : 4);

  let dayInfo = $derived.by(() => {
    if (!days) return null;
    const i = days.days.findIndex((d) => d.day === day);
    if (i < 0) return null;
    const d = days.days[i];
    const next = days.days[i + 1];
    return { start: d.s, end: next ? next.s : d.e + 8 * 3600 };
  });
  let channelById = $derived(new Map((data?.channels ?? []).map((c) => [c.id, c])));

  async function loadDays() {
    days = await get('/api/schedule/days');
    if (!day || !days.days.some((d) => d.day === day)) day = days.today;
  }
  async function loadSlots() {
    if (!dayInfo) { data = null; return; }
    loading = true;
    try {
      data = await get('/api/schedule', { start: dayInfo.start, end: dayInfo.end, ads: 0 });
    } finally {
      loading = false;
    }
  }

  $effect(() => {
    changes.schedule; // eslint-disable-line no-unused-expressions
    untrack(() => loadDays().then(loadSlots).catch(() => {}));
  });
  let firstScroll = true;
  $effect(() => {
    if (!dayInfo) return;
    untrack(() => loadSlots().then(() => {
      if (firstScroll && day === days?.today) { firstScroll = false; setTimeout(() => grid?.scrollTo(clock.ts, 80), 30); }
    }).catch(() => {}));
  });

  function pick(d) {
    day = d;
    setQuery({ day: d });
  }
  function goNow() {
    if (days && day !== days.today) { pick(days.today); setTimeout(() => grid?.scrollTo(clock.ts, 80), 300); }
    else grid?.scrollTo(clock.ts, 80);
  }
  function shift(n) {
    if (!days) return;
    const i = days.days.findIndex((d) => d.day === day);
    const t = days.days[i + n];
    if (t) pick(t.day);
  }
  let isCurrent = $derived(selected && selected.start_ts <= clock.ts && selected.end_ts > clock.ts);
</script>

<div class="page">
  <div class="page-head">
    <h1>Guide</h1>
    <div class="row">
      <button class="small" onclick={() => shift(-1)} disabled={!days || days.days[0]?.day === day} aria-label="Previous day">◀</button>
      <select value={day} onchange={(e) => pick(e.currentTarget.value)} aria-label="Day">
        {#each days?.days ?? [] as d (d.day)}
          <option value={d.day}>{fmtDay(d.day)}{d.day === days.today ? ' (today)' : ''}</option>
        {/each}
      </select>
      <button class="small" onclick={() => shift(1)} disabled={!days || days.days.at(-1)?.day === day} aria-label="Next day">▶</button>
      <button class="small primary" onclick={goNow}>Now</button>
      <span class="btn-group">
        <button class="small" onclick={() => grid?.scrollByMinutes(-120)} aria-label="Earlier">−2h</button>
        <button class="small" onclick={() => grid?.scrollByMinutes(120)} aria-label="Later">+2h</button>
      </span>
    </div>
  </div>

  {#if days && !days.days.length}
    <div class="empty">No schedule has been built yet. Build one from <a href="#/admin">Admin</a>.</div>
  {:else if dayInfo}
    <EpgGrid bind:this={grid} channels={data?.channels ?? []} slots={data?.slots ?? []}
             start={dayInfo.start} end={dayInfo.end} now={clock.ts} {ppm} {loading}
             selectedId={selected?.id ?? null} onselect={(s) => (selected = s)} />
    <p class="tiny muted mt">Dashed blocks are overnight replays. Times are local.</p>
  {:else}
    <div class="skeleton" style="height:300px"></div>
  {/if}
</div>

<Drawer open={!!selected} title={selected?.title ?? ''} subtitle={selected?.subtitle ?? ''} onclose={() => (selected = null)}>
  {#if selected}
    <div class="stack">
      <div class="row">
        <ChannelBadge channel={channelById.get(selected.channel_id)} size="sm" />
        <span class="muted">{fmtRange(selected.start_ts, selected.end_ts)}</span>
        {#if isCurrent}<span class="badge ok">On now</span>{/if}
        {#if selected.replay}<span class="badge">Replay</span>{/if}
        {#if selected.kind === 'filler'}<span class="badge">Filler</span>{/if}
      </div>
      <dl class="kv">
        {#if selected.year}<dt>Year</dt><dd>{selected.year}</dd>{/if}
        {#if selected.certificate}<dt>Certificate</dt><dd>{selected.certificate}</dd>{/if}
        <dt>Duration</dt><dd>{fmtDuration(selected.end_ts - selected.start_ts)}{selected.duration && Math.abs(selected.duration - (selected.end_ts - selected.start_ts)) > 90 ? ` (file ${fmtDuration(selected.duration)})` : ''}</dd>
        {#if selected.genres?.length}<dt>Genres</dt><dd>{selected.genres.join(', ')}</dd>{/if}
        {#if selected.season != null}<dt>Episode</dt><dd>S{String(selected.season).padStart(2, '0')}E{String(selected.episode ?? 0).padStart(2, '0')}</dd>{/if}
      </dl>
      {#if selected.plot}<p>{selected.plot}</p>{/if}
      {#if isCurrent}
        <button class="primary" disabled={!player.state.online}
                onclick={() => tryApi(post('/api/player/channel', { number: channelById.get(selected.channel_id)?.number }), { success: 'Tuned' })}>Watch now</button>
      {/if}
    </div>
  {/if}
</Drawer>
