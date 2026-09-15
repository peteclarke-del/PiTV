<script>
  import { untrack } from 'svelte';
  import { get, post, del, tryApi, confirmApi } from '../../lib/api.js';
  import { changes, clock, toast } from '../../lib/stores.svelte.js';
  import { fmtDay, fmtRange, fmtDuration, fmtDateTime, fmtTime, tsToLocalDay, tsToLocalTime, localToTs, plural } from '../../lib/format.js';
  import { dayBounds, pickDay, builtToday, defaultPpm } from '../../lib/schedule.js';
  import EpgGrid from '../../components/EpgGrid.svelte';
  import DayNav from '../../components/DayNav.svelte';
  import Drawer from '../../components/Drawer.svelte';
  import Modal from '../../components/Modal.svelte';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
  import MediaPicker from './MediaPicker.svelte';
  import JobList from './JobList.svelte';

  let days = $state(null);
  let day = $state('');
  let ads = $state(false);
  let data = $state(null);
  let loading = $state(false);
  let selected = $state(null);
  let grid = $state(null);
  const ppm = defaultPpm();
  let picker = $state(null);          // 'replace' | 'insert' | null
  let insert = $state(null);          // { channel_id, day, time, media_id, label }
  let notes = $state([]);             // notes from the last synchronous edit
  let busy = $state(false);

  let dayInfo = $derived(dayBounds(days, day));
  let channelById = $derived(new Map((data?.channels ?? []).map((c) => [c.id, c])));
  let editable = $derived(selected && selected.start_ts > clock.ts && !selected.replay);

  async function loadDays() {
    days = await get('/api/schedule/days');
    day = pickDay(days, day);
  }
  async function loadSlots() {
    if (!dayInfo) { data = null; return; }
    loading = true;
    try { data = await get('/api/schedule', { start: dayInfo.start, end: dayInfo.end, ads: ads ? 1 : 0 }); }
    finally { loading = false; }
  }
  // Every schedule change refetches the day list; a new dayInfo (new day or new bounds) or the ads toggle refetches the slots.
  $effect(() => { changes.schedule; untrack(() => loadDays().catch(() => {})); });
  let first = true;
  $effect(() => {
    if (!dayInfo) return;
    ads;
    untrack(() => loadSlots().then(() => {
      if (first) { first = false; if (day === days?.today) setTimeout(() => grid?.scrollTo(clock.ts, 80), 30); }
      if (selected) selected = data?.slots.find((s) => s.id === selected.id) ?? null;
    }).catch(() => {}));
  });
  function goNow() {
    const today = builtToday(days);
    if (today && day !== today) { day = today; setTimeout(() => grid?.scrollTo(clock.ts, 80), 300); }
    else grid?.scrollTo(clock.ts, 80);
  }

  function applyResult(r, msg) {
    if (!r) return;
    notes = r.notes ?? [];
    toast.success(r.summary ? `${msg}: ${r.summary}` : msg);
    selected = null;
    changes.schedule++;
  }
  async function lock(locked) {
    busy = true;
    const r = await tryApi(post(`/api/schedule/slots/${selected.id}/lock`, { locked }), { success: locked ? 'Slot locked' : 'Slot unlocked' });
    busy = false;
    if (r) { selected.locked = r.locked; changes.schedule++; }
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
    insert = { channel_id: slot?.channel_id ?? data?.channels[0]?.id, day: tsToLocalDay(ts), time: tsToLocalTime(ts), media_id: null, label: '' };
    selected = null;
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
  <div class="row">
    <DayNav {days} {day} onpick={(d) => (day = d)} />
    <button class="small" onclick={goNow}>Now</button>
    <label class="check small"><input type="checkbox" bind:checked={ads} /> Show ads &amp; idents</label>
    <span class="spacer"></span>
    <button class="small primary" onclick={() => openInsert(null)} disabled={!data}>Insert programme…</button>
  </div>

  {#if days && !days.days.length}
    <div class="empty">No schedule built yet. Use Build schedule on the dashboard.</div>
  {:else if dayInfo}
    <EpgGrid bind:this={grid} channels={data?.channels ?? []} slots={data?.slots ?? []} start={dayInfo.start} end={dayInfo.end}
             now={clock.ts} {ppm} {loading} editable selectedId={selected?.id ?? null} onselect={(s) => (selected = s)} />
    <p class="tiny muted">Click a future slot to lock, replace, remove or rebuild from it. Past and current slots and overnight replays are read-only.</p>
  {:else}
    <div class="skeleton" style="height:300px"></div>
  {/if}

  {#if notes.length}
    <div class="card"><div class="card-title"><h3>Notes from the last edit</h3><button class="small ghost" onclick={() => (notes = [])}>Clear</button></div><pre class="log">{notes.join('\n')}</pre></div>
  {/if}
  <div class="card"><div class="card-title"><h3>Build jobs</h3></div><JobList kind="schedule" limit={5} /></div>
</div>

<Drawer open={!!selected} title={selected?.title ?? ''} subtitle={selected?.subtitle ?? ''} onclose={() => (selected = null)}>
  {#if selected}
    <div class="stack">
      <div class="row">
        <ChannelBadge channel={channelById.get(selected.channel_id)} size="sm" />
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
        <p class="note">This slot has already started, so it can't be edited.</p>
      {/if}
    </div>
  {/if}
</Drawer>

<MediaPicker open={picker === 'replace'} onclose={() => (picker = null)} onpick={replaceWith} />
<MediaPicker open={picker === 'insert'} onclose={() => (picker = null)} onpick={(p) => { insert.media_id = p.media_id; insert.label = p.label; picker = null; }} />

<Modal open={!!insert && picker !== 'insert'} title="Insert programme" onclose={() => (insert = null)}>
  {#if insert}
    <div class="stack">
      <label class="field">Channel
        <select bind:value={insert.channel_id}>{#each data?.channels ?? [] as c (c.id)}<option value={c.id}>{c.number} {c.name}</option>{/each}</select>
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
