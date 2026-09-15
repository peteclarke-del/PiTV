<script>
  // One application and the machine it runs on. PiTV and pitv_content report the same document
  // (docs/CONTENT_CONTRACT.md, section 7), so either may sit on its own box.
  import AppBadge from './AppBadge.svelte';
  import { fmtBytes, fmtUptime } from '../lib/format.js';

  let { title, app, host = null, note = '', hostBadge = '', children } = $props();

  // Distribution builds carry their packaging suffix ("6.1.1-3ubuntu5+esm13"); the upstream
  // version is what matters at a glance, the full string is on hover.
  const upstream = (v) => (/^\d/.test(v) ? v.split(/[-+~]/)[0] : v);
</script>

<div class="card">
  <div class="card-title"><h3>{title}</h3><AppBadge {app} />{@render children?.()}</div>
  {#if host}
    <dl class="kv">
      <dt>Version</dt><dd>{host.version ?? '–'} · Python {host.python ?? '?'}</dd>
      {#each Object.entries(host.tools ?? {}) as [name, version] (name)}
        <dt>{name}</dt><dd title={version || ''}>{version ? upstream(version) : 'not found'}</dd>
      {/each}
      <dt>Host</dt><dd>{host.hostname ?? '–'}{host.uptime_s != null ? ` · up ${fmtUptime(host.uptime_s)}` : ''}
        {#if hostBadge}<div><span class="badge info">{hostBadge}</span></div>{/if}</dd>
      <dt>Machine</dt><dd>{host.model ?? '–'}</dd>
      <dt>Load</dt><dd>{host.load ? host.load.map((l) => l.toFixed(2)).join(' / ') : '–'}</dd>
      <dt>Temperature</dt><dd>{host.temperature_c != null ? `${host.temperature_c.toFixed(1)} °C` : '–'}</dd>
      <dt>Memory</dt><dd>{host.memory ? `${fmtBytes(host.memory.available)} free of ${fmtBytes(host.memory.total)}` : '–'}</dd>
    </dl>
  {:else if note}
    <p class="small muted">{note}</p>
  {:else}
    <div class="skeleton" style="height:100px"></div>
  {/if}
</div>
