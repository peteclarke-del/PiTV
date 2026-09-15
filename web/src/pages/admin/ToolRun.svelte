<script>
  import { toolPost } from '../../lib/toolapi.js';
  import { tryApi } from '../../lib/api.js';
  import { toast } from '../../lib/stores.svelte.js';

  let { running = false, onchange } = $props();
  let mode = $state('cache');
  let kind = $state('');
  let count = $state('');
  let url = $state('');
  let busy = $state(false);

  async function run() {
    busy = true;
    const body = { mode, kind: kind || null, count: count === '' ? null : Number(count), url: url.trim() || null };
    const r = await tryApi(toolPost('run', body));
    busy = false;
    if (r?.ok) { toast.success(`pitv_content run started (job ${r.job_id ?? '?'})`); onchange?.(); }
    else if (r) toast.error(r.error || 'Run was not started');
  }
  async function cancel() {
    busy = true;
    const r = await tryApi(toolPost('cancel'));
    busy = false;
    if (r?.ok) { toast.success('Cancel requested'); onchange?.(); }
  }
</script>

<div class="card">
  <div class="card-title"><h3>Run</h3>{#if running}<span class="badge info">running</span>{/if}</div>
  <div class="inline-form">
    <label class="field">Mode
      <select bind:value={mode}><option value="cache">cache – fill the Pi's cache for the schedule</option><option value="catalogue">catalogue – refresh the catalogue</option><option value="dry-run">dry-run – report only</option></select>
    </label>
    <label class="field">Kind
      <select bind:value={kind}><option value="">all</option><option value="shows">shows</option><option value="sport">sport</option><option value="music">music</option><option value="adverts">adverts</option></select>
    </label>
    <label class="field">Count<input class="xnarrow" type="number" min="1" bind:value={count} placeholder="all" /></label>
    <label class="field" style="flex:1;min-width:220px">URL<input bind:value={url} placeholder="optional: fetch this one page/file" /></label>
    <button class="primary" onclick={run} disabled={busy || running}>Run</button>
    {#if running}<button class="danger" onclick={cancel} disabled={busy}>Cancel</button>{/if}
  </div>
  {#if running}<p class="tiny muted mt">A run is active; wait for it to finish or cancel it before starting another.</p>{/if}
</div>
