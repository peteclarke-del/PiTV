<script>
  import AppBadge from '../../components/AppBadge.svelte';
  import { onMount } from 'svelte';
  import { confirmApi, get, post, tryApi } from '../../lib/api.js';
  import { isOffline, toolProbe } from '../../lib/toolapi.js';
  import HostCard from '../../components/HostCard.svelte';
  import DataTable from '../../components/DataTable.svelte';
  import { auth, toast } from '../../lib/stores.svelte.js';
  import { fmtBytes, fmtDateTime } from '../../lib/format.js';
  import { downloadJson } from '../../lib/util.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { poll } from '../../lib/poll.svelte.js';
  import JobList from './JobList.svelte';

  let info = $state(null);
  let error = $state('');
  let pw = $state({ current: '', password: '', confirm: '' });
  let busy = $state(false);

  async function load() {
    loadContent();
    streams = (await tryApi(get('/api/streams'))) ?? streams;
    try { info = await get('/api/system'); error = ''; } catch (e) { error = e.detail || e.message; }
  }

  // Who is watching a channel over HTTP, and what they are getting. Streams start on request and
  // stop when nobody asks, so an empty table is the normal state, not a fault.
  let streams = $state(null);

  // pitv_content's host comes from its own API, not from this machine, so the card stays right
  // when the two apps run on separate boxes.
  let content = $state(null);
  let contentNote = $state('');
  async function loadContent() {
    try {
      content = (await toolProbe('system', (d) => 'hostname' in d)) ?? null;
      contentNote = content ? '' : 'pitv_content API offline.';
    } catch (e) {
      content = null;
      contentNote = isOffline(e) ? 'pitv_content API offline.'
        : e.status === 404 ? 'This version of pitv_content does not report host details.' : (e.detail || e.message);
    }
  }
  onMount(load);

  const exportJson = guard(async () => {
    const data = await tryApi(get('/api/export'));
    if (data) downloadJson(data, `pitv-backup-${new Date().toISOString().slice(0, 10)}.json`);
  });

  // A restore overwrites the settings, channels, bands, sources and line-ups now in place, so it
  // asks first and then says what it actually did: an override for a title the library has not
  // been given yet is reported as waiting rather than silently dropped.
  let restoreFile = $state(null);
  let restoring = $state(false);
  async function restoreJson(e) {
    const file = e.target.files?.[0];
    e.target.value = '';
    if (!file) return;
    let doc;
    try { doc = JSON.parse(await file.text()); } catch { toast.error('That file is not JSON'); return; }
    restoring = true;
    const r = await confirmApi(
      `Restore ${file.name}? Settings, channels, bands, sources and line-ups are replaced by what the file holds.`,
      { title: 'Restore backup', okLabel: 'Restore', danger: true }, () => post('/api/import', doc));
    restoring = false;
    if (r) {
      const waiting = r.waiting ? `, ${r.waiting} override${r.waiting === 1 ? '' : 's'} waiting on the library` : '';
      toast.success(`Restored ${r.settings} settings, ${r.channels} channels, ${r.bands} bands, `
        + `${r.sources} sources, ${r.lineup_entries} line-up entries${waiting}`);
      load();
    }
  }

  async function changePassword(e) {
    e.preventDefault();
    if (pw.password !== pw.confirm) { toast.error('New passwords do not match'); return; }
    busy = true;
    const body = auth.password_set ? { current: pw.current, password: pw.password } : { password: pw.password };
    const r = await tryApi(post(auth.password_set ? '/api/auth/password' : '/api/auth/setup', body), { success: 'Password updated' });
    busy = false;
    if (r) { pw = { current: '', password: '', confirm: '' }; auth.password_set = true; auth.admin = true; }
  }
  const HEALTH = { ok: 'ok', idle: 'info', warn: 'warn', down: 'danger' };
  const HEALTH_ORDER = { down: 0, warn: 1, absent: 2, idle: 3, ok: 4 };
  const streamColumns = [
    { key: 'channel', label: 'Channel', class: 'num' },
    { key: 'address', label: 'Watching from', class: 'mono small' },
    { key: 'playing', label: 'Showing' },
    { key: 'idle_seconds', label: 'Last asked', class: 'small num', get: (r) => r.idle_seconds, cell: idleCell },
  ];

  const serviceColumns = [
    { key: 'unit', label: 'Service', cell: unitCell },
    { key: 'app', label: 'App', cell: appCell },
    { key: 'health', label: 'Status', get: (s) => HEALTH_ORDER[s.health], cell: stateCell },
    { key: 'responding', label: 'Answers', cell: answersCell },
    { key: 'since_ts', label: 'Up since', class: 'small', get: (s) => s.since_ts, cell: sinceCell },
    { key: 'memory', label: 'Memory', class: 'small num', cell: memoryCell },
    { key: 'restarts', label: 'Restarts', class: 'small num' },
    { key: 'actions', label: '', class: 'right nowrap', sortable: false, cell: actionsCell },
  ];
  const mountColumns = [
    { key: 'name', label: 'Mount', cell: boldName },
    { key: 'path', label: 'Path', class: 'mono small' },
    { key: 'available', label: 'Status', cell: mountStatus },
    { key: 'free', label: 'Free', class: 'small', get: (m) => m.usage?.free, cell: freeCell },
  ];
  let acting = $state('');
  let actingAll = $state(false);
  poll(load, 10000);

  // Stopping the player blanks the screen, and restarting the web service drops this page for a moment: ask first.
  async function serviceAction(unit, action) {
    const verb = { restart: 'Restart', stop: 'Stop', start: 'Start' }[action] ?? action;
    const call = () => post(`/api/system/service/${encodeURIComponent(unit)}/${encodeURIComponent(action)}`);
    const opts = { success: `${verb} requested for ${unit}` };
    acting = unit;
    if (action === 'stop' || unit === 'pitv-web.service') {
      await confirmApi(`${verb} ${unit}?`, { title: `${verb} service`, okLabel: verb, danger: action === 'stop' }, call, opts);
    } else {
      await tryApi(call(), opts);
    }
    acting = '';
    setTimeout(load, 1500);
  }

  async function allServices(action) {
    const verb = { restart: 'Restart', stop: 'Stop', start: 'Start' }[action] ?? action;
    actingAll = true;
    await confirmApi(`${verb} all controllable PiTV and pitv_content services in dependency order?`,
      { title: `${verb} all services`, okLabel: `${verb} all`, danger: action === 'stop' },
      () => post(`/api/system/services/${action}`), { success: `${verb} all services requested` });
    actingAll = false;
    setTimeout(load, 1800);
  }
</script>

<div class="stack">
  {#if error}<div class="badge danger">{error}</div>{/if}
  <div class="grid">
    <HostCard title="PiTV" app="pitv" host={info?.host}>
      <button class="small ghost" onclick={load}>Refresh</button>
    </HostCard>
    <HostCard title="pitv_content" app="content" host={content} note={contentNote}
      hostBadge={content && info?.host ? (content.hostname === info.host.hostname ? 'same machine as PiTV' : 'separate machine') : ''} />
    <div class="card">
      <div class="card-title"><h3>Time</h3><AppBadge app="pitv" /></div>
      {#if info}
        <dl class="kv">
          <dt>Local</dt><dd>{info.time.local}</dd>
          <dt>Timezone</dt><dd>{info.time.timezone || 'unknown'}</dd>
          <dt>NTP synced</dt><dd>{#if info.time.ntp === 'yes'}<span class="badge ok">yes</span>{:else}<span class="badge warn">{info.time.ntp || 'unknown'}</span>{/if}</dd>
          <dt>Server clock</dt><dd>{fmtDateTime(info.time.now)}</dd>
        </dl>
      {/if}
    </div>
    <div class="card">
      <div class="card-title"><h3>Data</h3><AppBadge app="pitv" /></div>
      {#if info}
        <dl class="kv">
          <dt>Directory</dt><dd class="mono small">{info.data.path}</dd>
          <dt>Database</dt><dd>{fmtBytes(info.data.db_size)}</dd>
          <dt>Cache</dt><dd class="mono small">{info.cache_dir || '(not configured)'}</dd>
          <dt>Free space</dt><dd>{info.data.free != null ? `${fmtBytes(info.data.free)} of ${fmtBytes(info.data.total)}` : '–'}</dd>
        </dl>
      {/if}
    </div>
    <div class="card">
      <div class="card-title"><h3>Backup</h3><AppBadge app="pitv" /></div>
      <p class="small muted">Everything you have set: settings, channels and their bands, sources, line-ups and every
        show or media override. Not the library or the cache, which come back from pitv_content's index.</p>
      <div class="row">
        <button onclick={exportJson} disabled={exportJson.busy}>Export</button>
        <button onclick={() => restoreFile.click()} disabled={restoring}>Restore…</button>
      </div>
      <input type="file" accept="application/json,.json" bind:this={restoreFile} onchange={restoreJson} hidden />
    </div>
    <div class="card">
      <div class="card-title"><h3>{auth.password_set ? 'Change admin password' : 'Set admin password'}</h3><AppBadge app="pitv" /></div>
      <form class="stack" onsubmit={changePassword}>
        {#if auth.password_set}
          <label class="field">Current password<input type="password" bind:value={pw.current} autocomplete="current-password" required /></label>
        {/if}
        <label class="field">New password<input type="password" bind:value={pw.password} autocomplete="new-password" minlength="6" required /></label>
        <label class="field">Confirm<input type="password" bind:value={pw.confirm} autocomplete="new-password" minlength="6" required /></label>
        <div><button class="primary" type="submit" disabled={busy}>Save password</button></div>
      </form>
    </div>
  </div>

  <div class="card pad-0">
    <div class="card-title" style="padding:.8rem 1rem 0"><h3>Services</h3><AppBadge app="pitv" /><AppBadge app="content" />
      <span class="spacer"></span>
      <button class="small ghost" onclick={() => allServices('start')} disabled={actingAll || !!acting}>Start all</button>
      <button class="small ghost" onclick={() => allServices('restart')} disabled={actingAll || !!acting}>Restart all</button>
      <button class="small danger" onclick={() => allServices('stop')} disabled={actingAll || !!acting}>Stop all</button>
    </div>
    <p class="scope" style="padding:0 1rem;margin:.2rem 0 .4rem">The systemd units of both applications. "Answers" is a live check that the process responds, so a service run by hand on a desktop still shows as up.</p>
    <DataTable id="system-services" columns={serviceColumns} rows={info?.services ?? null} key={(r) => r.unit} card={false} empty="No services reported." />
  </div>

  <div class="card pad-0">
    <div class="card-title" style="padding:.8rem 1rem 0"><h3>Streams</h3><AppBadge app="pitv" />
      {#if streams?.total_streams}<span class="badge ok">{streams.total_streams} running</span>{/if}
      {#if streams?.total_viewers}<span class="badge info">{streams.total_viewers} watching</span>{/if}
    </div>
    <p class="scope" style="padding:0 1rem;margin:.2rem 0 .4rem">Channels being watched over HTTP (/channel/3 in a browser, /channel/3.m3u8 in VLC). A stream starts when someone asks for it and stops when nobody has for a while; the log is in Logs, stream.</p>
    <DataTable id="system-streams" columns={streamColumns} rows={streams?.viewers ?? null} key={(r) => `${r.channel}:${r.address}`} card={false} empty="Nobody is watching a stream." />
    {#each (streams?.streams ?? []).filter((s) => s.error) as s (s.channel)}
      <p class="scope" style="padding:0 1rem;color:var(--danger)">Channel {s.channel}: {s.error}</p>
    {/each}
  </div>

  <div class="card pad-0">
    <div class="card-title" style="padding:.8rem 1rem 0"><h3>NAS mounts</h3><AppBadge app="content" /></div>
    <p class="scope" style="padding:0 1rem;margin:.2rem 0 .4rem">pitv_content's sources as mounted on the Pi. pitv_content indexes them; PiTV reads them only for NAS fallback playback.</p>
    <DataTable id="system-mounts" columns={mountColumns} rows={info?.mounts ?? null} key={(r) => r.path} card={false} empty="No NAS sources configured." />
  </div>

  <div class="card"><div class="card-title"><h3>Jobs</h3><AppBadge app="pitv" /></div><JobList /></div>
</div>

{#snippet unitCell(s)}<div class="mono">{s.unit}</div><div class="tiny muted">{s.role}</div>{/snippet}
{#snippet appCell(s)}<AppBadge app={s.app} />{/snippet}
{#snippet stateCell(s)}<span class="badge {HEALTH[s.health] ?? ''}">{s.state}</span>{#if s.enabled && s.enabled !== 'enabled' && s.enabled !== 'static'}<div class="tiny muted">{s.enabled}</div>{/if}{/snippet}
{#snippet answersCell(s)}{#if s.responding === true}<span class="badge ok">yes</span>{:else if s.responding === false}<span class="badge danger">no</span>{:else}<span class="muted">–</span>{/if}{/snippet}
{#snippet actionsCell(s)}{#each s.actions as a (a)}<button class="small ghost" class:danger={a === 'stop'} onclick={() => serviceAction(s.unit, a)} disabled={actingAll || acting === s.unit}>{a}</button>{/each}{/snippet}
{#snippet mountStatus(m)}{#if m.available}<span class="badge ok">mounted</span>{:else}<span class="badge danger">missing</span>{/if}{/snippet}
{#snippet idleCell(r)}{r.idle_seconds < 5 ? 'now' : `${r.idle_seconds}s ago`}{/snippet}

{#snippet sinceCell(s)}{s.since_ts ? fmtDateTime(s.since_ts) : '–'}{/snippet}
{#snippet memoryCell(s)}{s.memory != null ? fmtBytes(s.memory) : '–'}{/snippet}
{#snippet boldName(m)}<b>{m.name}</b>{/snippet}
{#snippet freeCell(m)}{m.usage ? `${fmtBytes(m.usage.free)} of ${fmtBytes(m.usage.total)}` : '–'}{/snippet}
