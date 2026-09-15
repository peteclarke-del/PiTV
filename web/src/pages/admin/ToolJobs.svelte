<script>
  import { fmtDateTime, fmtDuration } from '../../lib/format.js';
  import StatusBadge from '../../components/StatusBadge.svelte';
  import DataTable from '../../components/DataTable.svelte';

  let { jobs = [] } = $props();
  const length = (j) => (j.finished_ts && j.started_ts ? j.finished_ts - j.started_ts : null);
  const columns = [
    { key: 'started_ts', label: 'Started', class: 'nowrap small', cell: startedCell },
    { key: 'mode', label: 'Mode' },
    { key: 'kind', label: 'Kind', class: 'small', get: (j) => j.kind ?? 'all' },
    { key: 'status', label: 'Status', cell: statusCell },
    { key: 'length', label: 'Length', class: 'small muted', get: length, cell: lengthCell },
    { key: 'summary', label: 'Summary', class: 'small' },
  ];
</script>

<DataTable id="content-jobs" {columns} rows={jobs} key={(j) => j.job_id} sort={{ key: 'started_ts', dir: 'desc' }} empty="No runs recorded by pitv_content yet." />

{#snippet startedCell(j)}{fmtDateTime(j.started_ts)}{/snippet}
{#snippet statusCell(j)}<StatusBadge status={j.status} />{/snippet}
{#snippet lengthCell(j)}{length(j) !== null ? fmtDuration(length(j)) : j.status === 'running' ? 'running' : '–'}{/snippet}
