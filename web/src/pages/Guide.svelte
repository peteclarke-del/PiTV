<script>
  import { untrack } from 'svelte';
  import { get, post, tryApi } from '../lib/api.js';
  import { changes, clock, route, player } from '../lib/stores.svelte.js';
  import { setQuery } from '../lib/router.js';
  import { fmtRange, fmtDuration, fmtEpisode, plural } from '../lib/format.js';
  import { dayBounds, pickDay, builtToday, defaultPpm } from '../lib/schedule.js';
  import EpgGrid from '../components/EpgGrid.svelte';
  import DayNav from '../components/DayNav.svelte';
  import Drawer from '../components/Drawer.svelte';
  import ChannelBadge from '../components/ChannelBadge.svelte';

  let days = $state(null);          // /api/schedule/days
  let failed = $state(false);       // the day list could not be fetched (toasted)
  let day = $state(route.query.day || '');
  let data = $state(null);          // /api/schedule for the chosen day
  let loading = $state(false);
  let selected = $state(null);
  let grid = $state(null);
  const ppm = defaultPpm();

  let dayInfo = $derived(dayBounds(days, day));
  let channelById = $derived(new Map((data?.channels ?? []).map((c) => [c.id, c])));
  let isCurrent = $derived(selected && selected.start_ts <= clock.ts && selected.end_ts > clock.ts);

  async function loadDays() {
    const d = await tryApi(get('/api/schedule/days'));
    failed = !d;
    if (!d) return;
    days = d;
    day = pickDay(days, day);
  }
  async function loadSlots() {
    if (!dayInfo) { data = null; return; }
    loading = true;
    data = (await tryApi(get('/api/schedule', { start: dayInfo.start, end: dayInfo.end, ads: 0 }))) ?? data;
    loading = false;
  }
  // Every schedule change refetches the day list; a new dayInfo (new day or new bounds) refetches the slots.
  $effect(() => { changes.schedule; untrack(loadDays); });
  let firstScroll = true;
  $effect(() => {
    if (!dayInfo) return;
    untrack(() => loadSlots().then(() => {
      if (firstScroll && day === days?.today) { firstScroll = false; setTimeout(() => grid?.scrollTo(clock.ts, 80), 30); }
    }));
  });

  function pick(d) {
    day = d;
    setQuery({ day: d });
  }
  function goNow() {
    const today = builtToday(days);
    if (today && day !== today) { pick(today); setTimeout(() => grid?.scrollTo(clock.ts, 80), 300); }
    else grid?.scrollTo(clock.ts, 80);
  }
</script>

<div class="page">
  <div class="page-head">
    <h1>Guide</h1>
    <div class="row">
      <DayNav {days} {day} onpick={pick} />
      <button class="small primary" onclick={goNow}>Now</button>
      <span class="btn-group">
        <button class="small" onclick={() => grid?.scrollByMinutes(-120)} aria-label="Earlier">−2h</button>
        <button class="small" onclick={() => grid?.scrollByMinutes(120)} aria-label="Later">+2h</button>
      </span>
    </div>
  </div>

  {#if failed && !days}
    <div class="empty">The schedule could not be loaded. <button class="small" onclick={loadDays}>Retry</button></div>
  {:else if days && !days.days.length}
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
        {#if selected.block}<span class="badge info">{plural(selected.items ?? 1, 'video')}</span>{/if}
      </div>
      <dl class="kv">
        {#if selected.block && selected.video_title && selected.kind !== 'filler' && selected.video_title !== selected.block}<dt>Now playing</dt><dd>{selected.video_title}</dd>{/if}
        {#if selected.year && !selected.block}<dt>Year</dt><dd>{selected.year}</dd>{/if}
        {#if selected.certificate}<dt>Certificate</dt><dd>{selected.certificate}</dd>{/if}
        <dt>Duration</dt><dd>{fmtDuration(selected.end_ts - selected.start_ts)}{selected.duration && Math.abs(selected.duration - (selected.end_ts - selected.start_ts)) > 90 ? ` (file ${fmtDuration(selected.duration)})` : ''}</dd>
        {#if selected.genres?.length}<dt>Genres</dt><dd>{selected.genres.join(', ')}</dd>{/if}
        {#if selected.season != null}<dt>Episode</dt><dd>{fmtEpisode(selected.season, selected.episode)}</dd>{/if}
      </dl>
      {#if selected.plot}<p>{selected.plot}</p>{/if}
      {#if isCurrent}
        <button class="primary" disabled={!player.state.online}
                onclick={() => tryApi(post('/api/player/channel', { number: channelById.get(selected.channel_id)?.number }), { success: 'Tuned' })}>Watch now</button>
      {/if}
    </div>
  {/if}
</Drawer>
