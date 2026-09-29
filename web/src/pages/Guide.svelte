<script>
  import { tuneChannel } from '../lib/actions.js';
  import { clock, route, player } from '../lib/stores.svelte.js';
  import { setQuery } from '../lib/router.js';
  import { fmtRange, fmtDuration, fmtEpisode, fmtTime, plural } from '../lib/format.js';
  import { ScheduleDay } from '../lib/schedule.svelte.js';
  import EpgGrid from '../components/EpgGrid.svelte';
  import DayNav from '../components/DayNav.svelte';
  import Drawer from '../components/Drawer.svelte';
  import ChannelBadge from '../components/ChannelBadge.svelte';

  const view = new ScheduleDay({ day: route.query.day || '', onpick: (day) => setQuery({ day }) });
  let selected = $state(null);
  let isCurrent = $derived(selected && selected.start_ts <= clock.ts && selected.end_ts > clock.ts);
  // What is on in a band at this moment, from the parts the guide was given.
  let playing = $derived(isCurrent ? (selected.parts ?? []).findLast((p) => p.start_ts <= clock.ts) : null);
</script>

<div class="page guide">
  <div class="page-head">
    <h1>Guide</h1>
    <div class="row">
      <DayNav days={view.days} day={view.day} onpick={view.pick} />
      <button class="small primary" onclick={view.goNow}>Now</button>
      <span class="btn-group">
        <button class="small" onclick={() => { view.following = false; view.grid?.scrollByMinutes(-120); }} aria-label="Earlier">−2h</button>
        <button class="small" onclick={() => { view.following = false; view.grid?.scrollByMinutes(120); }} aria-label="Later">+2h</button>
      </span>
    </div>
  </div>

  {#if view.failed && !view.days}
    <div class="empty">The schedule could not be loaded. <button class="small" onclick={view.loadDays}>Retry</button></div>
  {:else if view.days && !view.days.days.length}
    <div class="empty">No schedule has been built yet. Build one from <a href="#/admin">Admin</a>.</div>
  {:else if view.bounds}
    <EpgGrid bind:this={view.grid} channels={view.data?.channels ?? []} slots={view.data?.slots ?? []}
             start={view.bounds.start} end={view.bounds.end} now={clock.ts} loading={view.loading} follow={view.following}
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
        <ChannelBadge channel={view.channelById.get(selected.channel_id)} size="sm" />
        <span class="muted">{fmtRange(selected.start_ts, selected.end_ts)}</span>
        {#if isCurrent}<span class="badge ok">On now</span>{/if}
        {#if selected.replay}<span class="badge">Replay</span>{/if}
        {#if selected.kind === 'filler'}<span class="badge">Filler</span>{/if}
        {#if selected.block && (selected.items ?? 1) > 1}<span class="badge info">{plural(selected.items, 'programme')}</span>{/if}
      </div>
      <dl class="kv">
        {#if playing}<dt>Now playing</dt><dd>{playing.title}{playing.subtitle ? `: ${playing.subtitle}` : ''}</dd>{/if}
        {#if selected.year && !selected.block}<dt>Year</dt><dd>{selected.year}</dd>{/if}
        {#if selected.certificate}<dt>Certificate</dt><dd>{selected.certificate}</dd>{/if}
        <dt>Duration</dt><dd>{fmtDuration(selected.end_ts - selected.start_ts)}{selected.duration && Math.abs(selected.duration - (selected.end_ts - selected.start_ts)) > 90 ? ` (file ${fmtDuration(selected.duration)})` : ''}</dd>
        {#if selected.genres?.length}<dt>Genres</dt><dd>{selected.genres.join(', ')}</dd>{/if}
        {#if selected.season != null}<dt>Episode</dt><dd>{fmtEpisode(selected.season, selected.episode)}</dd>{/if}
      </dl>
      {#if (selected.parts?.length ?? 0) > 1}
        <table class="parts">
          <tbody>
            {#each selected.parts as part (part.start_ts)}
              <tr class:on={part === playing}><td class="nowrap muted">{fmtTime(part.start_ts)}</td><td>{part.title}{part.subtitle ? `: ${part.subtitle}` : ''}</td></tr>
            {/each}
          </tbody>
        </table>
      {:else if selected.plot}<p>{selected.plot}</p>{/if}
      {#if isCurrent}
        <button class="primary" disabled={!player.state.online}
                onclick={() => tuneChannel(view.channelById.get(selected.channel_id)?.number)}>Watch now</button>
      {/if}
    </div>
  {/if}
</Drawer>

<style>
  .parts { font-size: .85rem; }
  .parts td { padding: .25rem .4rem; vertical-align: top; }
  .parts tr.on td { font-weight: 650; background: color-mix(in srgb, var(--accent) 10%, transparent); }
  /* The guide is a timetable: it takes the window's width, as the admin's tables do. */
  .guide { width: 100%; max-width: none; padding-inline: clamp(12px, 2vw, 32px); padding-bottom: 1rem; }
</style>
