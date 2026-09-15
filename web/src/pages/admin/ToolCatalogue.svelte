<script>
  import { toolPut } from '../../lib/toolapi.js';
  import { tryApi } from '../../lib/api.js';
  import { fmtAgo } from '../../lib/format.js';
  import { clock } from '../../lib/stores.svelte.js';

  let { catalogue = [], onchange } = $props();
  let filter = $state('');
  let busy = $state('');
  let groups = $derived.by(() => {
    const q = filter.trim().toLowerCase();
    const rows = catalogue.filter((c) => !q || c.name.toLowerCase().includes(q) || (c.kind ?? '').toLowerCase().includes(q));
    const m = new Map();
    for (const r of rows) { if (!m.has(r.kind)) m.set(r.kind, []); m.get(r.kind).push(r); }
    return [...m.entries()];
  });
  async function toggle(c, enabled) {
    busy = c.name;
    const r = await tryApi(toolPut('catalogue', { name: c.name, enabled }), { success: `${c.name} ${enabled ? 'enabled' : 'disabled'}` });
    busy = '';
    if (r) onchange?.();
  }
</script>

<div class="stack">
  <div class="row"><input type="search" placeholder="Filter catalogue…" bind:value={filter} style="max-width:320px" /><span class="small muted">{catalogue.length} entries · {catalogue.filter((c) => c.enabled).length} enabled</span></div>
  <div class="card pad-0 table-wrap">
    <table>
      <thead><tr><th>On</th><th>Name</th><th>Kind</th><th class="num">On disk</th><th>Last fetched</th></tr></thead>
      <tbody>
        {#each groups as [kind, rows] (kind)}
          <tr><td colspan="5" class="grp">{kind}</td></tr>
          {#each rows as c (c.name)}
            <tr>
              <td><input type="checkbox" checked={!!c.enabled} disabled={busy === c.name} onchange={(e) => toggle(c, e.currentTarget.checked)} aria-label="Enable {c.name}" /></td>
              <td><b>{c.name}</b></td>
              <td class="small">{c.kind}</td>
              <td class="num">{c.count_on_disk ?? 0}</td>
              <td class="small muted">{c.last_fetched_ts ? fmtAgo(c.last_fetched_ts, clock.ts) : 'never'}</td>
            </tr>
          {/each}
        {:else}
          <tr><td colspan="5" class="empty">{catalogue.length ? 'No entries match the filter.' : 'The catalogue is empty; run pitv_content in catalogue mode.'}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</div>

<style>
  .grp { background: var(--bg-sunken); font-size: .75rem; text-transform: uppercase; letter-spacing: .05em; color: var(--fg-muted); font-weight: 650; }
</style>
