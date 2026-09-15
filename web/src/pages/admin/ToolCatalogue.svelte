<script>
  import { toolPut } from '../../lib/toolapi.js';
  import { tryApi } from '../../lib/api.js';
  import { fmtAgo } from '../../lib/format.js';
  import { clock } from '../../lib/stores.svelte.js';
  import DataTable from '../../components/DataTable.svelte';

  let { catalogue = [], onchange } = $props();
  let busy = $state('');
  let enabled = $derived(catalogue.filter((c) => c.enabled).length);
  const columns = [
    { key: 'enabled', label: 'On', get: (c) => !!c.enabled, cell: enabledCell },
    { key: 'name', label: 'Name', cell: nameCell },
    { key: 'kind', label: 'Kind', class: 'small' },
    { key: 'count_on_disk', label: 'On disk', class: 'num', get: (c) => c.count_on_disk ?? 0 },
    { key: 'last_fetched_ts', label: 'Last fetched', class: 'small muted', cell: fetchedCell },
  ];
  async function toggle(c, on) {
    busy = c.name;
    const r = await tryApi(toolPut('catalogue', { name: c.name, enabled: on }), { success: `${c.name} ${on ? 'enabled' : 'disabled'}` });
    busy = '';
    if (r) onchange?.();
  }
</script>

<DataTable id="content-catalogue" {columns} rows={catalogue} key={(c) => c.name} search="Filter catalogue…" sort={{ key: 'kind', dir: 'asc' }}
  toolbar={counts} empty="The catalogue is empty; run pitv_content in catalogue mode." />

{#snippet counts()}<span class="small muted">{catalogue.length} entries · {enabled} enabled</span>{/snippet}
{#snippet enabledCell(c)}<input type="checkbox" checked={!!c.enabled} disabled={busy === c.name} onchange={(e) => toggle(c, e.currentTarget.checked)} aria-label="Enable {c.name}" />{/snippet}
{#snippet nameCell(c)}<b>{c.name}</b>{/snippet}
{#snippet fetchedCell(c)}{c.last_fetched_ts ? fmtAgo(c.last_fetched_ts, clock.ts) : 'never'}{/snippet}
