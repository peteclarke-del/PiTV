<script>
  import { onMount } from 'svelte';
  import { get, post, tryApi } from '../../lib/api.js';
  import { auth, toast } from '../../lib/stores.svelte.js';
  import { fmtBytes, fmtDateTime } from '../../lib/format.js';
  import JobList from './JobList.svelte';

  let info = $state(null);
  let error = $state('');
  let pw = $state({ current: '', password: '', confirm: '' });
  let busy = $state(false);

  async function load() {
    try { info = await get('/api/system'); error = ''; } catch (e) { error = e.detail || e.message; }
    if (info && info.content_provider === undefined) {
      // Older backends omit it from /api/system; fall back to the settings value.
      try { info.content_provider = (await get('/api/settings')).content_provider ?? ''; } catch { /* leave blank */ }
    }
  }
  onMount(load);

  async function exportJson() {
    const data = await tryApi(get('/api/export'));
    if (!data) return;
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `pitv-export-${new Date().toISOString().slice(0, 10)}.json`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
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
  const svcClass = (s) => (s === 'active' ? 'ok' : s === 'inactive' || s === 'failed' ? 'danger' : '');
</script>

<div class="stack">
  {#if error}<div class="badge danger">{error}</div>{/if}
  <div class="grid">
    <div class="card">
      <div class="card-title"><h3>PiTV</h3><button class="small ghost" onclick={load}>Refresh</button></div>
      {#if info}
        <dl class="kv">
          <dt>Version</dt><dd>{info.version} · Python {info.python}{info.content_provider ? ` · content: ${info.content_provider}` : ''}</dd>
          <dt>mpv</dt><dd>{info.mpv || 'not found'}</dd>
          <dt>Host</dt><dd>{info.hostname} · {info.uptime}</dd>
          <dt>Load</dt><dd>{info.load ? info.load.map((l) => l.toFixed(2)).join(' / ') : '–'}</dd>
          <dt>Temperature</dt><dd>{info.temperature_c ? `${info.temperature_c} °C` : '–'}</dd>
        </dl>
      {:else}<div class="skeleton" style="height:100px"></div>{/if}
    </div>
    <div class="card">
      <div class="card-title"><h3>Time</h3></div>
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
      <div class="card-title"><h3>Services</h3></div>
      {#if info}
        <dl class="kv">
          {#each Object.entries(info.services) as [name, state] (name)}
            <dt class="mono">{name}</dt><dd><span class="badge {svcClass(state)}">{state}</span></dd>
          {/each}
        </dl>
      {/if}
    </div>
    <div class="card">
      <div class="card-title"><h3>Data</h3></div>
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

  <div class="card pad-0 table-wrap">
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

  <div class="grid">
    <div class="card">
      <div class="card-title"><h3>Backup</h3></div>
      <p class="small muted">Download settings, channels, sources and every show/media override as JSON.</p>
      <button onclick={exportJson}>Export settings &amp; overrides</button>
    </div>
    <div class="card">
      <div class="card-title"><h3>{auth.password_set ? 'Change admin password' : 'Set admin password'}</h3></div>
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

  <div class="card"><div class="card-title"><h3>Jobs</h3></div><JobList /></div>
</div>
