<script>
  // One application and the machine it runs on. PiTV and pitv_content report the same document
  // (docs/CONTENT_CONTRACT.md, section 7), so either may sit on its own box.
  import AppBadge from './AppBadge.svelte';
  import { fmtBytes, fmtUptime } from '../lib/format.js';

  let { title, app, host = null, note = '', children } = $props();
</script>

<div class="card">
  <div class="card-title"><h3>{title}</h3><AppBadge {app} />{@render children?.()}</div>
  {#if host}
    <dl class="kv">
      <dt>Version</dt><dd>{host.version ?? '–'} · Python {host.python ?? '?'}</dd>
      {#each Object.entries(host.tools ?? {}) as [name, version] (name)}
        <dt>{name}</dt><dd>{version || 'not found'}</dd>
      {/each}
      <dt>Host</dt><dd>{host.hostname ?? '–'}{host.uptime_s != null ? ` · up ${fmtUptime(host.uptime_s)}` : ''}</dd>
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
