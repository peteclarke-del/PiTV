<script>
  import { untrack } from 'svelte';
  import { get, post, tryApi } from '../../lib/api.js';
  import { changes, jobs, confirm, clock } from '../../lib/stores.svelte.js';
  import { fmtDay, fmtDateTime, fmtAgo } from '../../lib/format.js';
  import JobList from './JobList.svelte';

  let summary = $state(null);
  let days = $state(null);
  let runs = $state([]);

  async function load() {
    try {
      const [s, d, j, r] = await Promise.all([
        get('/api/library/summary'), get('/api/schedule/days'), get('/api/jobs'), get('/api/runs', { limit: 5 }),
      ]);
      summary = s; days = d; runs = r;
      for (const job of j) {
        const i = jobs.list.findIndex((x) => x.id === job.id);
        if (i >= 0) jobs.list[i] = job; else jobs.list.push(job);
      }
    } catch { /* toasts come from actions; silent here */ }
  }
  $effect(() => { changes.library; changes.schedule; untrack(load); }); // eslint-disable-line no-unused-expressions

  const scan = () => tryApi(post('/api/scan'), { success: 'Scan started' });
  const build = () => tryApi(post('/api/schedule/build', {}), { success: 'Schedule build started' });
  async function rebuild() {
    if (await confirm('Throw away the whole generated week (locked slots included) and rebuild it from scratch?', { title: 'Rebuild week', okLabel: 'Rebuild', danger: true }))
      tryApi(post('/api/schedule/build', { force: true }), { success: 'Rebuild started' });
  }
  let daysLeft = $derived(days?.horizon_end ? Math.max(0, (days.horizon_end - clock.ts) / 86400) : null);
</script>

<div class="stack">
  <div class="row">
    <button class="primary" onclick={scan}>Scan all sources</button>
    <button onclick={build}>Build schedule</button>
    <button class="danger" onclick={rebuild}>Rebuild week</button>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-title"><h3>Library</h3><a class="small" href="#/admin/library">Open</a></div>
      {#if summary}
        <div class="stats">
          <div class="stat"><b>{summary.shows}</b><span>Shows</span></div>
          <div class="stat"><b>{summary.kinds.episode ?? 0}</b><span>Episodes</span></div>
          <div class="stat"><b>{summary.kinds.movie ?? 0}</b><span>Movies</span></div>
          <div class="stat"><b>{summary.kinds.advert ?? 0}</b><span>Adverts</span></div>
          <div class="stat"><b>{summary.kinds.ident ?? 0}</b><span>Idents</span></div>
          <div class="stat"><b>{summary.hours}</b><span>Hours</span></div>
        </div>
        <p class="small muted mt">{summary.hwdec} of {summary.programmes} programmes can be hardware decoded.
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
          <p class="small muted mt">{fmtDay(days.days[0].day)} → {fmtDay(days.days.at(-1).day)} · ends {fmtDateTime(days.horizon_end)}</p>
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
            <li><span class="badge {r.status === 'ok' ? 'ok' : r.status === 'running' ? 'info' : r.status === 'warning' ? 'warn' : 'danger'}">{r.kind}</span>
              <span class="small">{r.summary || r.status}</span><span class="tiny muted nowrap">{fmtAgo(r.started_at, clock.ts)}</span></li>
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

<style>
  .runs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .4rem; }
  .runs li { display: flex; gap: .5rem; align-items: baseline; }
  .runs li span:nth-child(2) { flex: 1; }
</style>
