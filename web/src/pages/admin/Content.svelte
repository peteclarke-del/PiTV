<script>
  import { onMount } from 'svelte';
  import { get, post, tryApi } from '../../lib/api.js';
  import { clock, toast } from '../../lib/stores.svelte.js';
  import { fmtAgo, fmtDateTime, fmtDuration } from '../../lib/format.js';
  import ProgressBar from '../../components/ProgressBar.svelte';
  import ManifestCard from './ManifestCard.svelte';
  import Logs from './Logs.svelte';

  let tool = $state(null);
  let error = $state('');
  let running = $state(false);
  let readiness = $state(null);
  let checking = $state(false);
  let timer;

  async function load() {
    try { tool = await get('/api/content/tool'); error = ''; } catch (e) { error = e.detail || e.message; }
  }
  onMount(() => { load(); timer = setInterval(load, 5000); return () => clearInterval(timer); });

  async function runNow() {
    running = true;
    const r = await tryApi(post('/api/content/tool/run', {}));
    running = false;
    if (r?.ok) toast.success('pitv_content started'); else if (r) toast.error(r.error || 'Could not start pitv_content');
    load();
  }
  async function checkReadiness() {
    checking = true;
    const r = await tryApi(post('/api/content/readiness', { days: 1, substitute: true }));
    checking = false;
    if (!r) return;
    readiness = r;
    (r.status === 'ok' ? toast.success : toast.error)(`Readiness ${r.status}: ${r.summary}`);
  }
  let st = $derived(tool?.status ?? null);
  let stateBadge = $derived(st?.state === 'running' ? 'info' : st?.state === 'failed' ? 'danger' : 'ok');
  const svc = (v) => (v === 'active' ? 'ok' : v === 'inactive' ? '' : 'warn');
  const runBadge = (v) => (v === 'ok' ? 'ok' : v === 'running' ? 'info' : v === 'warning' ? 'warn' : v ? 'danger' : '');
</script>

<div class="stack">
  {#if error}<div class="badge danger">{error}</div>{/if}
  <div class="card">
    <div class="card-title"><h3>pitv_content</h3>
      <button class="small" onclick={checkReadiness} disabled={checking}>{checking ? 'Checking…' : 'Check readiness'}</button>
      <button class="small primary" onclick={runNow} disabled={running || !tool}>Run now</button>
    </div>
    {#if tool}
      <div class="row small">
        {#if tool.installed}<span class="badge ok">installed{tool.version ? ` ${tool.version}` : ''}</span>{:else}<span class="badge warn">not installed</span>{/if}
        <span class="sep">·</span><span>service <span class="badge {svc(tool.service)}">{tool.service}</span></span>
        <span>timer <span class="badge {svc(tool.timer)}">{tool.timer}</span></span>
        {#if tool.running_marker}<span class="badge info">running marker present</span>{/if}
      </div>
      {#if tool.timers}<div class="tiny muted mono mt">{tool.timers}</div>{/if}
    {:else}<div class="skeleton" style="height:40px"></div>{/if}
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-title"><h3>Current run</h3>{#if st}<span class="badge {stateBadge}">{st.state}</span>{/if}</div>
      {#if !tool}<div class="skeleton" style="height:80px"></div>
      {:else if !st}
        <p class="muted small">No status file yet{tool.status_file ? ` at ${tool.status_file}` : ''}. pitv_content writes it while it runs.</p>
      {:else}
        <dl class="kv small">
          {#if st.phase}<dt>Phase</dt><dd>{st.phase}</dd>{/if}
          {#if st.started_ts}<dt>Started</dt><dd>{fmtDateTime(st.started_ts)}{st.updated_ts ? ` · updated ${fmtAgo(st.updated_ts, clock.ts)}` : ''}</dd>{/if}
          {#if st.finished_ts}<dt>Finished</dt><dd>{fmtDateTime(st.finished_ts)}</dd>{/if}
          {#if st.next_timer_ts}<dt>Next run</dt><dd>{fmtDateTime(st.next_timer_ts)}</dd>{/if}
        </dl>
        {#if st.current}
          <div class="mt small"><span class="badge info">{st.current.action ?? st.current.kind}</span> <b>{st.current.title ?? `#${st.current.id}`}</b>{st.current.eta_s ? ` · ${fmtDuration(st.current.eta_s)} left` : ''}</div>
          <ProgressBar value={st.current.progress ?? 0} />
        {/if}
        {#if st.counts}
          <div class="stats mt">
            <div class="stat"><b>{st.counts.items_done ?? 0}<span class="of">/{st.counts.items_total ?? 0}</span></b><span>Items done</span></div>
            <div class="stat"><b>{st.counts.items_failed ?? 0}</b><span>Failed</span></div>
            <div class="stat"><b>{st.counts.items_skipped ?? 0}</b><span>Skipped</span></div>
            <div class="stat"><b>{st.counts.wanted_done ?? 0}<span class="of">/{st.counts.wanted_total ?? 0}</span></b><span>Wanted</span></div>
            <div class="stat"><b>{st.counts.wanted_failed ?? 0}</b><span>Wanted failed</span></div>
          </div>
        {/if}
        {#if st.errors?.length}
          <h4 class="mt">Errors</h4>
          <pre class="log">{st.errors.join('\n')}</pre>
        {/if}
      {/if}
    </div>

    <div class="card">
      <div class="card-title"><h3>Last run</h3></div>
      {#if st?.last_run}
        <div class="row small"><span class="badge {runBadge(st.last_run.status)}">{st.last_run.status}</span><span>{st.last_run.summary}</span></div>
        <div class="tiny muted mt">{fmtDateTime(st.last_run.started_ts)}{st.last_run.finished_ts ? ` → ${fmtDateTime(st.last_run.finished_ts)}` : ''}</div>
      {:else}<p class="muted small">No completed run reported.</p>{/if}
      <h4 class="mt">Reports</h4>
      {#if tool?.reports?.length}
        <ul class="runs">{#each tool.reports as r (r.id)}<li><span class="badge {runBadge(r.status)}">{r.status}</span><span class="small">{r.summary || '–'}</span><span class="tiny muted nowrap">{fmtAgo(r.started_at, clock.ts)}</span></li>{/each}</ul>
      {:else}<p class="muted small">No reports yet; the tool posts one after each run.</p>{/if}
      {#if readiness}
        <h4 class="mt">Readiness</h4>
        <div class="row small"><span class="badge {runBadge(readiness.status)}">{readiness.status}</span><span>{readiness.summary}</span></div>
        {#if readiness.notes?.length}<pre class="log mt">{readiness.notes.join('\n')}</pre>{/if}
      {/if}
    </div>
  </div>

  <ManifestCard />

  <div class="card">
    <div class="card-title"><h3>pitv_content log</h3></div>
    <Logs fixed="pitv-content" height="360px" />
  </div>
</div>

<style>
  .sep { color: var(--fg-faint); }
  .of { font-size: .9rem; font-weight: 500; color: var(--fg-muted); }
  .runs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .4rem; }
  .runs li { display: flex; gap: .5rem; align-items: baseline; }
  .runs li span:nth-child(2) { flex: 1; }
</style>
