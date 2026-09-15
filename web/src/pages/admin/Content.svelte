<script>
  // Admin tab for pitv_content, the support app on the Pi that fetches, encodes and fills the cache. Two sources of truth: PiTV's
  // file-based view (GET /api/content/tool: status file, systemd state) and, when it answers, the
  // app's own API proxied at /api/content/tool/api/*; the live document wins where both exist.
  import { onMount, untrack } from 'svelte';
  import { get, post, tryApi } from '../../lib/api.js';
  import { toolGet, toolPut, toolProbe, isOffline, fieldErrors } from '../../lib/toolapi.js';
  import { checkReadiness } from '../../lib/actions.js';
  import { clock, toast, route } from '../../lib/stores.svelte.js';
  import { navigate } from '../../lib/router.js';
  import { fmtAgo, fmtDateTime, fmtDuration } from '../../lib/format.js';
  import { poll } from '../../lib/poll.svelte.js';
  import { guard } from '../../lib/guard.svelte.js';
  import ProgressBar from '../../components/ProgressBar.svelte';
  import SchemaForm from '../../components/SchemaForm.svelte';
  import StatusBadge from '../../components/StatusBadge.svelte';
  import AppBadge from '../../components/AppBadge.svelte';
  import ReadinessNotes from '../../components/ReadinessNotes.svelte';
  import ManifestCard from './ManifestCard.svelte';
  import Tabs from '../../components/Tabs.svelte';
  import DataTable from '../../components/DataTable.svelte';
  import ContentTokenCard from './ContentTokenCard.svelte';
  import Logs from './Logs.svelte';
  import ToolRun from './ToolRun.svelte';
  import ToolCatalogue from './ToolCatalogue.svelte';
  import ToolJobs from './ToolJobs.svelte';

  // Unknown sub-paths (including #/admin/content/providers, now its own admin tab) show the status view.
  const SUBTABS = [['overview', 'Status', 'basic'], ['run', 'Run', 'standard'], ['settings', 'Settings', 'standard'],
    ['catalogue', 'Online catalogue', 'standard'], ['jobs', 'Jobs', 'advanced'], ['log', 'Log', 'advanced']];
  const NEEDS_API = ['settings', 'catalogue', 'jobs'];
  let sub = $derived(SUBTABS.some(([id]) => id === route.parts[2]) ? route.parts[2] : 'overview');
  let tool = $state(null);          // GET /api/content/tool
  let live = $state(null);          // the app's own status document, null while unreachable
  let online = $derived(live !== null);
  let toolUrl = $state('');
  let error = $state('');
  let readiness = $state(null);
  let schema = $state([]);
  let schemaErrors = $state({});
  let savingSchema = $state(false);
  let catalogue = $state(null);
  let jobs = $state(null);
  let loadedSub = $state({});

  async function loadStatus() {
    try { tool = await get('/api/content/tool'); error = ''; } catch (e) { error = e.detail || e.message; }
    try {
      live = (await toolProbe('status', (s) => 'state' in s || 'tool' in s || 'api_version' in s)) ?? null;
    } catch (e) {
      live = null;
      if (!isOffline(e)) toast.error(`pitv_content status: ${e.detail || e.message}`);
    }
  }
  async function loadSub(which, force = false) {
    if (!online || (!force && loadedSub[which])) return;
    try {
      if (which === 'settings') { const r = await toolGet('settings'); schema = r?.schema ?? []; schemaErrors = {}; }
      else if (which === 'catalogue') catalogue = (await toolGet('catalogue')) ?? [];
      else if (which === 'jobs') jobs = (await toolGet('jobs', { limit: 20 })) ?? [];
      loadedSub[which] = true;
    } catch (e) { if (!isOffline(e)) toast.error(e.detail || e.message); }
  }
  onMount(async () => {
    toolUrl = (await tryApi(get('/api/settings')))?.content_tool_url ?? '';
    loadStatus();
  });
  poll(loadStatus, 5000);
  $effect(() => { const w = sub; if (online && NEEDS_API.includes(w)) untrack(() => loadSub(w)); });

  async function saveSchema(body) {
    savingSchema = true; schemaErrors = {};
    try {
      const r = await toolPut('settings', body);
      schema = r?.schema ?? schema; loadedSub.settings = true;
      toast.success('pitv_content settings saved');
    } catch (e) {
      const fe = fieldErrors(e);
      if (fe) { schemaErrors = fe; toast.error('Some settings were rejected'); } else toast.error(e.detail || e.message);
    }
    savingSchema = false;
  }
  const readinessCheck = guard(async () => { readiness = (await checkReadiness()) ?? readiness; });
  const runNow = guard(async () => {
    const r = await tryApi(post('/api/content/tool/run', {}));
    if (r?.ok) toast.success('pitv_content started'); else if (r) toast.error(r.error || 'Could not start pitv_content');
    loadStatus();
  });
  let st = $derived(live ?? tool?.status ?? null);
  let isRunning = $derived(st?.state === 'running');
  let stateBadge = $derived(st?.state === 'running' ? 'info' : st?.state === 'failed' ? 'danger' : 'ok');
  // systemd unit state: the timer-driven service is normally inactive, so only odd states are amber.
  const svc = (v) => (v === 'active' ? 'ok' : v === 'inactive' ? '' : 'warn');
  const REPORT_COLUMNS = [
    { key: 'started_at', label: 'When', class: 'small nowrap', cell: reportWhen },
    { key: 'status', label: 'Status', cell: reportStatus },
    { key: 'summary', label: 'Summary', class: 'small' },
  ];
</script>

<div class="stack">
  {#if error}<div class="badge danger">{error}</div>{/if}
  {#if !online}
    <div class="warn-box">pitv_content API not reachable{toolUrl ? ` at ${toolUrl}` : ''}; is <code>pitv-content-api.service</code> running? Showing the file-based status and log meanwhile.</div>
  {/if}

  <div class="card">
    <div class="card-title"><h3>pitv_content</h3><AppBadge app="content" />
      {#if online}<span class="badge ok">API online{live?.version ? ` · ${live.version}` : ''}{live?.api_version ? ` (api ${live.api_version})` : ''}</span>{:else}<span class="badge warn">API offline</span>{/if}
      <span class="spacer"></span>
      <button class="small primary" onclick={runNow} disabled={runNow.busy || !tool || isRunning}>Run now</button>
    </div>
    <p class="scope">The support app that indexes the NAS, fetches what is not on it, encodes to the Pi's profile and fills the cache. Everything on this page is pitv_content's, except the manifest and readiness, which are PiTV's side of the exchange.</p>
    {#if tool}
      <div class="row small">
        {#if tool.installed}<span class="badge ok">installed{tool.version ? ` ${tool.version}` : ''}</span>{:else}<span class="badge warn">not installed</span>{/if}
        <span class="sep">·</span><span>service <span class="badge {svc(tool.service)}">{tool.service}</span></span>
        <span>timer <span class="badge {svc(tool.timer)}">{tool.timer}</span></span>
        {#if tool.running_marker}<span class="badge info">running marker present</span>{/if}
      </div>
      {#if tool.next_run_ts}<div class="tiny muted mt">Next scheduled run {fmtDateTime(tool.next_run_ts)}</div>{/if}
    {:else}<div class="skeleton" style="height:40px"></div>{/if}
  </div>

  <Tabs active={sub} onselect={(id) => navigate(`/admin/content/${id}`)} label="pitv_content sections"
    tabs={SUBTABS.map(([id, label, level]) => ({ id, label, level, disabled: !online && NEEDS_API.includes(id), title: !online && NEEDS_API.includes(id) ? 'Needs the pitv_content API' : '' }))} />

  {#if sub === 'overview'}
    <div class="overview">
      <div class="card">
        <div class="card-title"><h3>Current run</h3>{#if st}<span class="badge {stateBadge}">{st.state}</span>{/if}{#if live}<span class="tiny muted">live</span>{/if}<AppBadge app="content" /></div>
        {#if !tool && !live}<div class="skeleton" style="height:80px"></div>
        {:else if !st}
          <p class="muted small">No status file yet{tool?.status_file ? ` at ${tool.status_file}` : ''}. pitv_content writes it while it runs.</p>
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
          {#if st.errors?.length}<h4 class="mt">Errors</h4><pre class="log">{st.errors.join('\n')}</pre>{/if}
        {/if}
      </div>

      <div class="card">
        <div class="card-title"><h3>Last run</h3><AppBadge app="content" /></div>
        {#if st?.last_run}
          <div class="row small"><StatusBadge status={st.last_run.status} /><span>{st.last_run.summary}</span></div>
          <div class="tiny muted mt">{fmtDateTime(st.last_run.started_ts)}{st.last_run.finished_ts ? ` to ${fmtDateTime(st.last_run.finished_ts)}` : ''}</div>
        {:else}<p class="muted small">No completed run reported.</p>{/if}
        <h4 class="mt">Reports</h4>
        <DataTable id="content-reports" columns={REPORT_COLUMNS} rows={tool ? tool.reports ?? [] : null} card={false} size={10}
          sort={{ key: 'started_at', dir: 'desc' }} empty="No reports yet; the tool posts one after each run." />
      </div>
    </div>
    <div class="card">
      <div class="card-title"><h3>Readiness</h3><AppBadge app="pitv" /><span class="spacer"></span><button class="small" onclick={readinessCheck} disabled={readinessCheck.busy}>{readinessCheck.busy ? 'Checking…' : 'Check readiness'}</button></div>
      <p class="scope">PiTV verifies that tomorrow's airings are playable from the cache, or from the NAS while nas_fallback is on, and replaces anything that is not.</p>
      {#if readiness}
        <div class="row small mb"><StatusBadge status={readiness.status} /><span>{readiness.summary}</span></div>
        <ReadinessNotes notes={readiness.notes} />
      {:else}<p class="muted small">Runs automatically at the readiness hours set under Settings, Maintenance.</p>{/if}
    </div>
    <ManifestCard />
    <ContentTokenCard />
  {:else if sub === 'run'}
    {#if online}<ToolRun running={isRunning} onchange={loadStatus} />{:else}<div class="empty">Starting runs with options needs the pitv_content API. Run now above still starts the service.</div>{/if}
  {:else if sub === 'settings'}
    {#if !online}<div class="empty">Settings need the pitv_content API.</div>
    {:else if !loadedSub.settings}<div class="skeleton" style="height:200px"></div>
    {:else}<p class="scope" style="margin:0"><AppBadge app="content" /> pitv_content's own configuration, stored by pitv_content. PiTV relays the edits through its API; nothing here changes PiTV.</p><SchemaForm {schema} groups={['Providers', 'Search', 'Encoding', 'Schedule', 'Advanced']} errors={schemaErrors} saving={savingSchema} onsave={saveSchema} app="content" />{/if}
  {:else if sub === 'catalogue'}
    <p class="scope" style="margin:0"><AppBadge app="content" /> Online titles pitv_content knows about. Enabling one lets pitv_content fetch from it; anything fetched reaches PiTV's catalogue through the library index.</p>
    {#if !online}<div class="empty">The catalogue needs the pitv_content API.</div>{:else if !catalogue}<div class="skeleton" style="height:120px"></div>{:else}<ToolCatalogue {catalogue} onchange={() => loadSub('catalogue', true)} />{/if}
  {:else if sub === 'jobs'}
    <p class="scope" style="margin:0"><AppBadge app="content" /> pitv_content's own run history: index, copy, transcode and fetch jobs.</p>
    {#if !online}<div class="empty">Job history needs the pitv_content API.</div>{:else if !jobs}<div class="skeleton" style="height:120px"></div>{:else}<div class="row"><span class="spacer"></span><button class="small" onclick={() => loadSub('jobs', true)}>Refresh</button></div><ToolJobs {jobs} />{/if}
  {:else if sub === 'log'}
    <div class="card"><div class="card-title"><h3>pitv_content log</h3>{#if online}<span class="badge ok">live</span>{:else}<span class="badge">file</span>{/if}<AppBadge app="content" /></div><Logs fixed="pitv-content" height="480px" liveTool={online} /></div>
  {/if}
</div>

<style>
  .of { font-size: .9rem; font-weight: 500; color: var(--fg-muted); }
  .overview { display: grid; gap: 1rem; grid-template-columns: 1fr; }
  @media (min-width: 900px) { .overview { grid-template-columns: 3fr 2fr; align-items: start; } }
</style>

{#snippet reportWhen(r)}{fmtAgo(r.started_at, clock.ts)}{/snippet}
{#snippet reportStatus(r)}<StatusBadge status={r.status} />{/snippet}
