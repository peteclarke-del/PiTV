<script>
  import AppBadge from '../../components/AppBadge.svelte';
  import { post, del, tryApi, confirmApi } from '../../lib/api.js';
  import { clock, toast, noteChange } from '../../lib/stores.svelte.js';
  import { fmtDay, fmtRange, fmtDuration, fmtDateTime, fmtTime, tsToLocalDay, tsToLocalTime, localToTs, plural } from '../../lib/format.js';
  import { ScheduleDay } from '../../lib/schedule.svelte.js';
  import EpgGrid from '../../components/EpgGrid.svelte';
  import DayNav from '../../components/DayNav.svelte';
  import Drawer from '../../components/Drawer.svelte';
  import Modal from '../../components/Modal.svelte';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
  import MediaPicker from './MediaPicker.svelte';
  import JobList from './JobList.svelte';

  let ads = $state(false);
  let bandItems = $state(false);
  let selected = $state(null);
  let picker = $state(null);          // 'replace' | 'insert' | null
  let insert = $state(null);          // { channel_id, day, time, media_id, label }
  let notes = $state([]);             // notes from the last synchronous edit
  let busy = $state(false);
  // A refetch replaces the slot objects, so the open drawer follows its slot into the new list.
  const view = new ScheduleDay({
    ads: () => ads,
    bands: () => bandItems,
    onload: (data) => { if (selected) selected = data?.slots.find((s) => s.id === selected.id) ?? null; },
  });

  let editable = $derived(selected && selected.start_ts > clock.ts && !selected.replay);

  function applyResult(r, msg) {
    if (!r) return;
    notes = r.notes ?? [];
    toast.success(r.summary ? `${msg}: ${r.summary}` : msg);
    selected = null;
    noteChange('schedule');
  }
  async function lock(locked) {
    busy = true;
    const r = await tryApi(post(`/api/schedule/slots/${selected.id}/lock`, { locked }), { success: locked ? 'Slot locked' : 'Slot unlocked' });
    busy = false;
    if (r) { selected.locked = r.locked; noteChange('schedule'); }
  }
  async function remove() {
    busy = true;
    applyResult(await confirmApi(`Remove "${selected.title}" and rebuild the rest of the day on this channel?`, { title: 'Remove slot', okLabel: 'Remove', danger: true },
      () => del(`/api/schedule/slots/${selected.id}`)), 'Slot removed');
    busy = false;
  }
  async function replaceWith(pick) {
    picker = null;
    busy = true;
    applyResult(await tryApi(post(`/api/schedule/slots/${selected.id}/replace`, { media_id: pick.media_id })), `Replaced with ${pick.label}`);
    busy = false;
  }
  async function rebuildFromHere() {
    busy = true;
    applyResult(await confirmApi(`Regenerate this channel from ${fmtTime(selected.start_ts)} onward? Locked slots are kept.`, { title: 'Rebuild from here', okLabel: 'Rebuild' },
      () => post('/api/schedule/rebuild', { channel_id: selected.channel_id, from_ts: selected.start_ts })), 'Rebuilt');
    busy = false;
  }
  function openInsert(slot) {
    const ts = slot ? slot.start_ts : Math.ceil((clock.ts + 600) / 300) * 300;
    insert = { channel_id: slot?.channel_id ?? view.data?.channels[0]?.id, day: tsToLocalDay(ts), time: tsToLocalTime(ts), media_id: null, label: '' };
    selected = null;
  }
  // Settings and channel changes only reach a day that is built again: the build leaves days
  // that already have programmes alone, so this is how an edit is put into effect today.
  let rebuildChannel = $state('');
  async function rebuildDay() {
    const where = rebuildChannel ? `channel ${rebuildChannel}` : 'every channel';
    busy = true;
    const r = await confirmApi(`Rebuild ${fmtDay(view.day)} for ${where}? Slots already started and locked slots are kept.`,
      { title: 'Rebuild this day', okLabel: 'Rebuild' },
      () => post('/api/schedule/build', { start_day: view.day, days: 1, force: true,
                                          channels: rebuildChannel ? [Number(rebuildChannel)] : [] }));
    busy = false;
    if (r) { toast.success('Rebuilding; the day updates as it goes'); noteChange('schedule'); }
  }

  async function freshRebuild() {
    busy = true;
    const r = await confirmApi(
      'Stop pitv_content and have it publish a fresh index, then delete the whole generated schedule (past and locked slots included), the viewing history and every wanted request, and rebuild the full horizon from the current settings? Channels, settings, sources, line-ups and every file, fetched or cached, are kept.',
      { title: 'Fresh rebuild schedule', okLabel: 'Delete & rebuild', danger: true },
      () => post('/api/schedule/fresh-rebuild'));
    busy = false;
    if (r) { toast.success('Fresh rebuild started; the schedule will repopulate as it runs'); noteChange('schedule'); }
  }

  async function doInsert() {
    const start_ts = localToTs(insert.day, insert.time);
    busy = true;
    const r = await tryApi(post('/api/schedule/insert', { channel_id: Number(insert.channel_id), start_ts, media_id: insert.media_id }));
    busy = false;
    if (r) { insert = null; applyResult(r, 'Programme inserted'); }
  }
</script>

<div class="stack">
  <div class="row" style="gap:.5rem"><h2 style="margin:0">Schedule</h2><AppBadge app="pitv" /><span class="small muted">PiTV's editable schedule; pitv_content is asked for anything it needs to play.</span></div>
  <div class="row">
    <DayNav days={view.days} day={view.day} onpick={view.pick} />
    <button class="small" onclick={view.goNow}>Now</button>
    <label class="check small"><input type="checkbox" bind:checked={ads} /> Show ads &amp; idents</label>
    <label class="check small"><input type="checkbox" bind:checked={bandItems} /> Show band items</label>
    <span class="spacer"></span>
    <select class="small" bind:value={rebuildChannel} disabled={!view.data} aria-label="Channel to rebuild">
      <option value="">every channel</option>
      {#each view.data?.channels ?? [] as c (c.id)}<option value={c.number}>{c.number} {c.name}</option>{/each}
    </select>
    <button class="small" onclick={rebuildDay} disabled={busy || !view.day}>Rebuild this day</button>
    <button class="small danger" onclick={freshRebuild} disabled={busy}>Fresh rebuild…</button>
    <button class="small primary" onclick={() => openInsert(null)} disabled={!view.data}>Insert programme…</button>
  </div>

  {#if view.failed && !view.days}
    <div class="empty">The schedule could not be loaded. <button class="small" onclick={view.loadDays}>Retry</button></div>
  {:else if view.days && !view.days.days.length}
    <div class="empty">No schedule built yet. Use Build schedule on the dashboard.</div>
  {:else if view.bounds}
    <EpgGrid bind:this={view.grid} channels={view.data?.channels ?? []} slots={view.data?.slots ?? []} start={view.bounds.start} end={view.bounds.end}
             now={clock.ts} ppm={view.ppm} loading={view.loading} editable selectedId={selected?.id ?? null} onselect={(s) => (selected = s)} />
    <p class="tiny muted">Click a future slot to lock, replace, remove or rebuild from it. Past and current slots and overnight replays are read-only.</p>
  {:else}
    <div class="skeleton" style="height:300px"></div>
  {/if}

  {#if notes.length}
    <div class="card"><div class="card-title"><h3>Notes from the last edit</h3><AppBadge app="pitv" /><button class="small ghost" onclick={() => (notes = [])}>Clear</button></div><pre class="log">{notes.join('\n')}</pre></div>
  {/if}
  <div class="card"><div class="card-title"><h3>Build jobs</h3><AppBadge app="pitv" /></div><JobList kind="schedule" limit={5} /></div>
</div>

<Drawer open={!!selected} title={selected?.title ?? ''} subtitle={selected?.subtitle ?? ''} onclose={() => (selected = null)}>
  {#if selected}
    <div class="stack">
      <p class="scope" style="margin:0"><AppBadge app="pitv" /> Changes PiTV's schedule for this channel.</p>
      <div class="row">
        <ChannelBadge channel={view.channelById.get(selected.channel_id)} size="sm" />
        <span class="muted">{fmtRange(selected.start_ts, selected.end_ts)}</span>
        <span class="badge">{selected.kind}</span>
        {#if selected.locked}<span class="badge info">locked</span>{/if}
        {#if selected.replay}<span class="badge">replay</span>{/if}
        {#if selected.block}<span class="badge info">{selected.items ? plural(selected.items, 'video') : `block: ${selected.block}`}</span>{/if}
      </div>
      <dl class="kv small">
        <dt>Day</dt><dd>{fmtDay(selected.day)}</dd>
        <dt>Duration</dt><dd>{fmtDuration(selected.end_ts - selected.start_ts)}</dd>
        {#if selected.year}<dt>Year</dt><dd>{selected.year}</dd>{/if}
        {#if selected.certificate}<dt>Certificate</dt><dd>{selected.certificate}</dd>{/if}
        {#if selected.genres?.length}<dt>Genres</dt><dd>{selected.genres.join(', ')}</dd>{/if}
        {#if selected.hwdec === 0}<dt>Decode</dt><dd><span class="badge warn">software</span></dd>{/if}
      </dl>
      {#if selected.plot}<p class="small">{selected.plot}</p>{/if}
      {#if editable}
        <div class="btn-group">
          <button onclick={() => lock(!selected.locked)} disabled={busy}>{selected.locked ? 'Unlock' : 'Lock'}</button>
          <button onclick={() => (picker = 'replace')} disabled={busy}>Replace…</button>
          <button onclick={() => openInsert(selected)} disabled={busy}>Insert before…</button>
          <button onclick={rebuildFromHere} disabled={busy}>Rebuild from here</button>
          <button class="danger" onclick={remove} disabled={busy}>Remove</button>
        </div>
      {:else if selected.replay}
        <p class="note">Overnight replays follow the day's schedule; edit the original slot instead.</p>
      {:else}
        <p class="note">This slot has already started, so it cannot be edited.</p>
      {/if}
    </div>
  {/if}
</Drawer>

<MediaPicker open={picker === 'replace'} onclose={() => (picker = null)} onpick={replaceWith} />
<MediaPicker open={picker === 'insert'} onclose={() => (picker = null)} onpick={(p) => { insert.media_id = p.media_id; insert.label = p.label; picker = null; }} />

<Modal open={!!insert && picker !== 'insert'} title="Insert programme" onclose={() => (insert = null)}>
  {#if insert}
    <div class="stack">
      <p class="scope" style="margin:0"><AppBadge app="pitv" /> Adds a programme to PiTV's schedule.</p>
      <label class="field">Channel
        <select bind:value={insert.channel_id}>{#each view.data?.channels ?? [] as c (c.id)}<option value={c.id}>{c.number} {c.name}</option>{/each}</select>
      </label>
      <div class="row">
        <label class="field">Date<input type="date" bind:value={insert.day} /></label>
        <label class="field">Start<input type="time" bind:value={insert.time} /></label>
      </div>
      <div class="field"><span>Programme</span>
        <div class="row"><span class="truncate" style="flex:1">{insert.label || 'Nothing chosen'}</span><button onclick={() => (picker = 'insert')}>Choose…</button></div>
      </div>
      <p class="tiny muted">Whatever was scheduled in that gap is dropped and the rest of the day is rebuilt around the inserted programme. Locked slots block the insert.</p>
    </div>
  {/if}
  {#snippet footer()}
    <button onclick={() => (insert = null)}>Cancel</button>
    <button class="primary" onclick={doInsert} disabled={busy || !insert?.media_id || !insert?.day || !insert?.time}>Insert at {insert ? fmtDateTime(localToTs(insert.day, insert.time)) : ''}</button>
  {/snippet}
</Modal>
