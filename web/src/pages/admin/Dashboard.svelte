<script>
  import { untrack } from 'svelte';
  import { get, tryApi, confirmApi, upsertJob } from '../../lib/api.js';
  import { importCatalogue, buildSchedule, checkReadiness } from '../../lib/actions.js';
  import { changes, clock } from '../../lib/stores.svelte.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { fmtDay, fmtDateTime, fmtAgo, hasLineup } from '../../lib/format.js';
  import StatusBadge from '../../components/StatusBadge.svelte';
  import AppBadge from '../../components/AppBadge.svelte';
  import ReadinessNotes from '../../components/ReadinessNotes.svelte';
  import JobList from './JobList.svelte';

  let summary = $state(null);
  let days = $state(null);
  let runs = $state([]);
  let lineup = $state(null);      // {perChannel, total, placeholders, noChannel}
  let readiness = $state(null);

  async function load() {
    // Two batches in parallel so a failing line-up summary does not blank the rest of the page.
    const [r, l] = await Promise.all([
      tryApi(Promise.all([get('/api/library/summary'), get('/api/schedule/days'), get('/api/jobs'), get('/api/runs', { limit: 5 })])),
      tryApi(Promise.all([get('/api/lineup'), get('/api/library/attention'), get('/api/channels')])),
    ]);
    if (r) {
      let jobList;
      [summary, days, jobList, runs] = r;
      jobList.forEach(upsertJob); // jobs that finished before this page connected to the event stream
    }
    if (!l) return;
    const [entries, attention, channels] = l;
    const per = new Map();
    for (const c of channels) if (c.enabled && hasLineup(c)) per.set(c.id, { label: `${c.number} ${c.short_name}`, n: 0 });
    let placeholders = 0;
    for (const e of entries) { if (per.has(e.channel_id)) per.get(e.channel_id).n++; placeholders += e.placeholders ?? 0; }
    const noChannel = [...new Set(attention.filter((a) => String(a.attention ?? '').startsWith('No channel accepts')).map((a) => a.show_title || a.title))];
    lineup = { perChannel: [...per.values()], total: entries.length, placeholders, noChannel };
  }
  $effect(() => { changes.library; changes.schedule; untrack(load); });

  const importNow = guard(() => importCatalogue(false));
  const build = guard(() => buildSchedule());
  const readinessCheck = guard(async () => {
    const r = await checkReadiness();
    if (r) { readiness = r; load(); }
  });
  const rebuild = guard(() => confirmApi('Throw away the whole generated week (locked slots included) and rebuild it from scratch?',
    { title: 'Rebuild week', okLabel: 'Rebuild', danger: true }, () => buildSchedule(true)));
  let daysLeft = $derived(days?.horizon_end ? Math.max(0, (days.horizon_end - clock.ts) / 86400) : null);
</script>

<div class="stack">
  <div class="row">
    <button class="primary" onclick={importNow} disabled={importNow.busy} title="Import pitv_content's library index into the catalogue">Import catalogue</button>
    <button onclick={build} disabled={build.busy}>Build schedule</button>
    <button class="danger" onclick={rebuild} disabled={rebuild.busy}>Rebuild week</button>
    <button onclick={readinessCheck} disabled={readinessCheck.busy}>{readinessCheck.busy ? 'Checking…' : 'Check readiness'}</button>
  </div>
  {#if readiness}
    <div class="card">
      <div class="card-title"><h3>Readiness</h3><StatusBadge status={readiness.status} /><AppBadge app="pitv" /><button class="small ghost" onclick={() => (readiness = null)} aria-label="Dismiss">✕</button></div>
      <p class="small">{readiness.summary}</p>
      {#if readiness.notes?.length}<ReadinessNotes notes={readiness.notes} />{:else}<p class="tiny muted">No notes: every airing through tomorrow is playable from the cache.</p>{/if}
    </div>
  {/if}

  <div class="grid">
    <div class="card">
      <div class="card-title"><h3>Catalogue</h3><AppBadge app="pitv" /><a class="small" href="#/admin/library">Open</a></div>
      {#if summary}
        <div class="stats">
          <div class="stat"><b>{summary.shows}</b><span>Shows</span></div>
          <div class="stat"><b>{summary.kinds.episode ?? 0}</b><span>Episodes</span></div>
          <div class="stat"><b>{summary.kinds.movie ?? 0}</b><span>Movies</span></div>
          <div class="stat"><b>{summary.kinds.music ?? 0}</b><span>Music videos</span></div>
          <div class="stat"><b>{summary.kinds.advert ?? 0}</b><span>Adverts</span></div>
          <div class="stat"><b>{summary.kinds.ident ?? 0}</b><span>Idents</span></div>
          <div class="stat"><b>{summary.hours}</b><span>Hours</span></div>
        </div>
        <p class="small muted mt">{summary.hwdec} of {summary.programmes} programmes can be hardware decoded{summary.concerts ? `; ${summary.concerts} concerts` : ''}.
          {#if summary.attention}<a href="#/admin/library/attention" class="badge warn">{summary.attention} need attention</a>{:else}<span class="badge ok">Nothing needs attention</span>{/if}</p>
        <div class="row small mt" style="gap:.35rem">
          <span class="badge ok" title="Plays from local disk">{summary.cached ?? 0} cached</span>
          <span class="badge" title="pitv_content copies these to the cache before they air">{summary.nas_only ?? 0} NAS only</span>
          <span class="badge info" title="Fetched online by pitv_content">{summary.online ?? 0} online</span>
        </div>
        <p class="tiny muted mt">Everything scheduled is copied to the cache by pitv_content before it airs.
          {#if summary.last_import}Last import {fmtAgo(summary.last_import.finished_at ?? summary.last_import.started_at, clock.ts)}: <StatusBadge status={summary.last_import.status} /> {summary.last_import.summary}{:else}No catalogue import yet.{/if}</p>
      {:else}<div class="skeleton" style="height:80px"></div>{/if}
    </div>

    <div class="card">
      <div class="card-title"><h3>Schedule horizon</h3><AppBadge app="pitv" /><a class="small" href="#/admin/schedule">Open</a></div>
      {#if days}
        {#if days.days.length}
          <div class="stats">
            <div class="stat"><b>{days.days.length}</b><span>Days built</span></div>
            <div class="stat"><b>{daysLeft?.toFixed(1)}</b><span>Days left</span></div>
          </div>
          <p class="small muted mt">{fmtDay(days.days[0].day)} to {fmtDay(days.days.at(-1).day)} · ends {fmtDateTime(days.horizon_end)}</p>
        {:else}
          <p class="muted">No schedule built yet.</p>
        {/if}
      {:else}<div class="skeleton" style="height:80px"></div>{/if}
    </div>

    <div class="card">
      <div class="card-title"><h3>Line-ups</h3><AppBadge app="pitv" /><a class="small" href="#/admin/channels">Open</a></div>
      {#if lineup}
        {#if lineup.total}
          <div class="row" style="gap:.35rem">{#each lineup.perChannel as c (c.label)}<span class="chip">{c.label} <b>{c.n}</b></span>{/each}</div>
          <p class="small muted mt">{lineup.total} entries · {lineup.placeholders} external airing{lineup.placeholders === 1 ? '' : 's'} not yet fetched</p>
        {:else}
          <p class="muted small">No line-ups yet. Generate on the Channels page gives every series and film a channel.</p>
        {/if}
        {#if lineup.noChannel.length}
          <div class="warn-box mt"><b>No channel accepts:</b> {lineup.noChannel.slice(0, 8).join(', ')}{lineup.noChannel.length > 8 ? ` and ${lineup.noChannel.length - 8} more` : ''}<div class="tiny">Widen a channel's allowed genres, or add them to a line-up by hand.</div></div>
        {/if}
      {:else}<div class="skeleton" style="height:60px"></div>{/if}
    </div>

    <div class="card">
      <div class="card-title"><h3>Recent runs</h3><AppBadge app="pitv" /></div>
      <p class="scope">PiTV's run log: catalogue imports, schedule builds, readiness checks and pitv_content's delivery reports as PiTV received them.</p>
      {#if runs.length}
        <ul class="runs">
          {#each runs as r (r.id)}
            <li><StatusBadge status={r.status} label={r.kind} /><span class="small">{r.summary || r.status}</span><span class="tiny muted nowrap">{fmtAgo(r.started_at, clock.ts)}</span></li>
          {/each}
        </ul>
      {:else}<p class="muted small">No import or build has run yet.</p>{/if}
    </div>
  </div>

  <div class="card">
    <div class="card-title"><h3>Jobs</h3><AppBadge app="pitv" /></div>
    <JobList />
  </div>
</div>
