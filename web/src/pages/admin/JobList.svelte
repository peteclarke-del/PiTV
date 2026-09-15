<script>
  import { jobs } from '../../lib/stores.svelte.js';
  import ProgressBar from '../../components/ProgressBar.svelte';
  import StatusBadge from '../../components/StatusBadge.svelte';

  let { limit = 8, kind = null } = $props();
  let list = $derived([...jobs.list].filter((j) => !kind || j.kind === kind).reverse().slice(0, limit));
  let open = $state({});
</script>

{#if !list.length}
  <p class="muted small">No jobs have run since the service started.</p>
{:else}
  <ul class="jobs">
    {#each list as j (j.id)}
      <li>
        <div class="row">
          <StatusBadge status={j.status} />
          <b>{j.label}</b>
          <span class="muted small truncate" style="flex:1">{j.message}{j.total ? ` (${j.done}/${j.total})` : ''}</span>
          {#if j.notes?.length || j.result?.notes?.length || j.error}
            <button class="small ghost" onclick={() => (open[j.id] = !open[j.id])}>{open[j.id] ? 'Hide' : 'Details'}</button>
          {/if}
        </div>
        {#if j.status === 'running'}<ProgressBar value={j.total ? j.done / j.total : 0} />{/if}
        {#if j.status === 'done' && j.result?.summary}<div class="small muted">{j.result.summary}</div>{/if}
        {#if j.status === 'done' && j.result && !j.result.summary && j.kind === 'schedule'}
          <div class="small muted">{j.result.built ?? ''}{j.result.days ? ` ${j.result.days} day(s)` : ''}{j.result.notes ? ` · ${j.result.notes.length} note(s)` : ''}</div>
        {/if}
        {#if open[j.id]}
          <pre class="log">{[j.error, ...(j.notes ?? []), ...((j.result?.notes && !j.notes?.length) ? j.result.notes : [])].filter(Boolean).join('\n') || 'No notes.'}</pre>
        {/if}
      </li>
    {/each}
  </ul>
{/if}

<style>
  .jobs { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .6rem; }
  .jobs li { display: flex; flex-direction: column; gap: .3rem; padding-bottom: .5rem; border-bottom: 1px solid var(--border); }
  .jobs li:last-child { border-bottom: 0; }
</style>
