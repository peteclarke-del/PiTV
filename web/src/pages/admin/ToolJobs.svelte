<script>
  import { fmtDateTime, fmtDuration } from '../../lib/format.js';
  import StatusBadge from '../../components/StatusBadge.svelte';
  let { jobs = [] } = $props();
</script>

<div class="card pad-0 table-wrap">
  <table>
    <thead><tr><th>Started</th><th>Mode</th><th>Kind</th><th>Status</th><th>Length</th><th>Summary</th></tr></thead>
    <tbody>
      {#each jobs as j (j.job_id)}
        <tr>
          <td class="nowrap small">{fmtDateTime(j.started_ts)}</td>
          <td>{j.mode}</td>
          <td class="small">{j.kind ?? 'all'}</td>
          <td><StatusBadge status={j.status} /></td>
          <td class="small muted">{j.finished_ts && j.started_ts ? fmtDuration(j.finished_ts - j.started_ts) : j.status === 'running' ? 'running' : '–'}</td>
          <td class="small">{j.summary ?? ''}</td>
        </tr>
      {:else}
        <tr><td colspan="6" class="empty">No runs recorded by pitv_content yet.</td></tr>
      {/each}
    </tbody>
  </table>
</div>
