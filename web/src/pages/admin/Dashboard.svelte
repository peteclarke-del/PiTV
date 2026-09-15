<script>
  import { untrack } from 'svelte';
  import { get, tryApi, confirmApi, upsertJob } from '../../lib/api.js';
  import { scanAll, buildSchedule, checkReadiness } from '../../lib/actions.js';
  import { changes, clock } from '../../lib/stores.svelte.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { fmtDay, fmtDateTime, fmtAgo } from '../../lib/format.js';
  import StatusBadge from '../../components/StatusBadge.svelte';
  import JobList from './JobList.svelte';

  let summary = $state(null);
  let days = $state(null);
  let runs = $state([]);
  let readiness = $state(null);

  async function load() {
    const r = await tryApi(Promise.all([
      get('/api/library/summary'), get('/api/schedule/days'), get('/api/jobs'), get('/api/runs', { limit: 5 }),
    ]));
    if (!r) return;
    [summary, days] = r;
    runs = r[3];
    r[2].forEach(upsertJob); // jobs that finished before this page connected to the event stream
  }
  $effect(() => { changes.library; changes.schedule; untrack(load); });

  const scan = guard(scanAll);
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
    <button class="primary" onclick={scan} disabled={scan.busy}>Scan all sources</button>
    <button onclick={build} disabled={build.busy}>Build schedule</button>
    <button class="danger" onclick={rebuild} disabled={rebuild.busy}>Rebuild week</button>
    <button onclick={readinessCheck} disabled={readinessCheck.busy}>{readinessCheck.busy ? 'Checking…' : 'Check readiness'}</button>
  </div>
  {#if readiness}
    <div class="card">
      <div class="card-title"><h3>Readiness</h3><StatusBadge status={readiness.status} /><button class="small ghost" onclick={() => (readiness = null)} aria-label="Dismiss">✕</button></div>
      <p class="small">{readiness.summary}</p>
      {#if readiness.notes?.length}<pre class="log">{readiness.notes.join('\n')}</pre>{:else}<p class="tiny muted">No notes: every programme for tomorrow is reachable.</p>{/if}
    </div>
  {/if}

  <div class="grid">
    <div class="card">
      <div class="card-title"><h3>Library</h3><a class="small" href="#/admin/library">Open</a></div>
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
      {:else}<div class="skeleton" style="height:80px"></div>{/if}
    </div>

    <div class="card">
      <div class="card-title"><h3>Schedule horizon</h3><a class="small" href="#/admin/schedule">Open</a></div>
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
      <div class="card-title"><h3>Recent runs</h3></div>
      {#if runs.length}
        <ul class="runs">
          {#each runs as r (r.id)}
            <li><StatusBadge status={r.status} label={r.kind} /><span class="small">{r.summary || r.status}</span><span class="tiny muted nowrap">{fmtAgo(r.started_at, clock.ts)}</span></li>
          {/each}
        </ul>
      {:else}<p class="muted small">No scan or build has run yet.</p>{/if}
    </div>
  </div>

  <div class="card">
    <div class="card-title"><h3>Jobs</h3></div>
    <JobList />
  </div>
</div>
