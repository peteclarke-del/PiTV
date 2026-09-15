<script>
  import AppBadge from '../../components/AppBadge.svelte';
  import { onMount } from 'svelte';
  import { ApiError, confirmApi, get, post, tryApi } from '../../lib/api.js';
  import { isOffline, toolGet } from '../../lib/toolapi.js';
  import HostCard from '../../components/HostCard.svelte';
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
    try { info = await get('/api/system'); error = ''; } catch (e) { error = e.detail || e.message; }
  }

  // pitv_content's host comes from its own API, not from this machine, so the card stays right
  // when the two apps run on separate boxes. While it is unreachable it is probed every 30 s:
  // each failed probe is a 503 the browser logs on the console.
  let content = $state(null);
  let contentNote = $state('');
  let contentProbeAt = 0;
  async function loadContent() {
    if (!content && contentNote && Date.now() - contentProbeAt < 30000) return;
    contentProbeAt = Date.now();
    try {
      const doc = await toolGet('system');
      if (!doc || typeof doc !== 'object' || !('hostname' in doc)) throw new ApiError(0, 'not pitv_content');
      content = doc;
      contentNote = '';
    } catch (e) {
      content = null;
      contentNote = isOffline(e) ? 'pitv_content API offline.'
        : e.status === 404 ? 'This version of pitv_content does not report host details.' : (e.detail || e.message);
    }
  }
  onMount(load);

  const exportJson = guard(async () => {
    const data = await tryApi(get('/api/export'));
    if (data) downloadJson(data, `pitv-export-${new Date().toISOString().slice(0, 10)}.json`);
  });

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
  let acting = $state('');
  poll(load, 10000);

  // Stopping the player blanks the screen, and restarting the web service drops this page for a moment: ask first.
  async function serviceAction(unit, action) {
    const verb = { restart: 'Restart', stop: 'Stop', start: 'Start' }[action] ?? action;
    const call = () => post(`/api/system/service/${unit}/${action}`);
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
</script>

<div class="stack">
  {#if error}<div class="badge danger">{error}</div>{/if}
  <div class="grid">
    <HostCard title="PiTV" app="pitv" host={info?.host}>
      <button class="small ghost" onclick={load}>Refresh</button>
    </HostCard>
    <HostCard title="pitv_content" app="content" host={content} note={contentNote}>
      {#if content && info?.host}
        <span class="badge {content.hostname === info.host.hostname ? 'info' : ''}">{content.hostname === info.host.hostname ? 'same machine as PiTV' : 'separate machine'}</span>
      {/if}
    </HostCard>
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
  </div>

  <div class="card pad-0">
    <div class="card-title" style="padding:.8rem 1rem 0"><h3>Services</h3><AppBadge app="pitv" /><AppBadge app="content" /></div>
    <p class="scope" style="padding:0 1rem;margin:.2rem 0 .4rem">The systemd units of both applications. "Answers" is a live check that the process responds, so a service run by hand on a desktop still shows as up.</p>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Service</th><th>App</th><th>Status</th><th>Answers</th><th>Up since</th><th>Memory</th><th>Restarts</th><th></th></tr></thead>
      <tbody>
        {#each info?.services ?? [] as s (s.unit)}
          <tr>
            <td><div class="mono">{s.unit}</div><div class="tiny muted">{s.role}</div></td>
            <td><AppBadge app={s.app} /></td>
            <td><span class="badge {HEALTH[s.health] ?? ''}">{s.state}</span>{#if s.enabled && s.enabled !== 'enabled' && s.enabled !== 'static'}<div class="tiny muted">{s.enabled}</div>{/if}</td>
            <td>{#if s.responding === true}<span class="badge ok">yes</span>{:else if s.responding === false}<span class="badge danger">no</span>{:else}<span class="muted">–</span>{/if}</td>
            <td class="small">{s.since_ts ? fmtDateTime(s.since_ts) : '–'}</td>
            <td class="small">{s.memory != null ? fmtBytes(s.memory) : '–'}</td>
            <td class="small">{s.restarts ?? '–'}</td>
            <td class="right nowrap">
              {#each s.actions as a (a)}
                <button class="small ghost" onclick={() => serviceAction(s.unit, a)} disabled={acting === s.unit}>{a}</button>
              {/each}
            </td>
          </tr>
        {:else}
          <tr><td colspan="8" class="empty">{info ? 'No services reported.' : 'Loading…'}</td></tr>
        {/each}
      </tbody>
    </table>
    </div>
  </div>

  <div class="card pad-0">
    <div class="card-title" style="padding:.8rem 1rem 0"><h3>NAS mounts</h3><AppBadge app="content" /></div>
    <p class="scope" style="padding:0 1rem;margin:.2rem 0 .4rem">pitv_content's sources as mounted on the Pi. pitv_content indexes them; PiTV reads them only for NAS fallback playback.</p>
    <div class="table-wrap">
    <table>
      <thead><tr><th>Mount</th><th>Path</th><th>Status</th><th>Free</th></tr></thead>
      <tbody>
        {#each info?.mounts ?? [] as m (m.path)}
          <tr><td><b>{m.name}</b></td><td class="mono small">{m.path}</td>
            <td>{#if m.available}<span class="badge ok">mounted</span>{:else}<span class="badge danger">missing</span>{/if}</td>
            <td class="small">{m.usage ? `${fmtBytes(m.usage.free)} of ${fmtBytes(m.usage.total)}` : '–'}</td></tr>
        {:else}
          <tr><td colspan="4" class="empty">No sources configured.</td></tr>
        {/each}
      </tbody>
    </table>
    </div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="card-title"><h3>Backup</h3><AppBadge app="pitv" /></div>
      <p class="small muted">Download settings, channels, sources and every show/media override as JSON.</p>
      <button onclick={exportJson} disabled={exportJson.busy}>Export settings &amp; overrides</button>
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

  <div class="card"><div class="card-title"><h3>Jobs</h3><AppBadge app="pitv" /></div><JobList /></div>
</div>
