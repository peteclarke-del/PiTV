<script>
  // PiTV's catalogue is imported from pitv_content's library index; PiTV itself never scans the NAS.
  import { untrack } from 'svelte';
  import { get, post, tryApi, confirmApi } from '../../lib/api.js';
  import { importCatalogue } from '../../lib/actions.js';
  import { changes, clock, toast } from '../../lib/stores.svelte.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { fmtAgo, fmtDateTime } from '../../lib/format.js';
  import { downloadJson } from '../../lib/util.js';
  import AppBadge from '../../components/AppBadge.svelte';
  import StatusBadge from '../../components/StatusBadge.svelte';
  import JobList from './JobList.svelte';

  let cat = $state(null);   // {last_import, index_file, index_file_exists}
  let fileInput = $state(null);
  async function load() { cat = (await tryApi(get('/api/catalogue'))) ?? cat; }
  $effect(() => { changes.library; untrack(load); });

  let li = $derived(cat?.last_import ?? null);
  // run_log.details arrives as a JSON string on this endpoint; accept either form.
  let details = $derived.by(() => {
    const d = li?.details;
    if (Array.isArray(d)) return d;
    try { const v = JSON.parse(d ?? '[]'); return Array.isArray(v) ? v : []; } catch { return d ? [String(d)] : []; }
  });
  const importNow = guard(() => importCatalogue(false));
  const reindex = guard(() => importCatalogue(true));
  const enrich = guard(async () => {
    const r = await tryApi(post('/api/catalogue/enrich', { limit: 500, force: true }), { success: 'Online certificate checks started' });
    if (r) toast.success('Missing ratings are being checked; progress appears below');
  });
  const exportNow = guard(async () => {
    const doc = await tryApi(get('/api/catalogue/export'));
    if (doc) downloadJson(doc, 'catalogue.json');
  });
  async function importFile(e) {
    const file = e.currentTarget.files?.[0]; e.currentTarget.value = '';
    if (!file) return;
    let doc;
    try { doc = JSON.parse(await file.text()); } catch { toast.error('That file is not JSON'); return; }
    const r = await confirmApi(`Import "${file.name}" as pitv_content's library index? A complete index marks anything it does not list as missing.`,
      { title: 'Import index file', okLabel: 'Import' }, () => post('/api/catalogue/import', doc));
    if (r) toast.success('Index file import started; progress below');
  }
</script>

<div class="card">
  <div class="card-title"><h3>Catalogue</h3><AppBadge app="pitv" />
    <span class="spacer"></span>
    <button class="small primary" onclick={importNow} disabled={importNow.busy}>Import catalogue</button>
    <button class="small" onclick={reindex} disabled={reindex.busy} title="pitv_content re-scans its sources first, then PiTV imports the new index">Re-index sources and import</button>
    <button class="small" onclick={enrich} disabled={enrich.busy} title="Look up unrated films and series through pitv_content; exact title/year matches only">Check missing ratings</button>
    <button class="small ghost" onclick={() => fileInput?.click()} title="Development: import an index document from a file">Import file…</button>
    <button class="small ghost" onclick={exportNow} disabled={exportNow.busy}>Export</button>
    <input type="file" accept="application/json,.json" bind:this={fileInput} onchange={importFile} hidden />
  </div>
  <p class="scope">What PiTV can schedule: imported from pitv_content's library index daily at the catalogue hour. Trusted online enrichment fills missing metadata; your admin overrides always win.</p>
  {#if cat}
    <div class="row small">
      {#if li}<StatusBadge status={li.status} /><span>{li.summary}</span><span class="tiny muted">{fmtAgo(li.finished_at ?? li.started_at, clock.ts)}</span>
      {:else}<span class="muted">No import yet. Import catalogue pulls pitv_content's index.</span>{/if}
    </div>
    <div class="tiny muted mt">Index file: {#if cat.index_file}<span class="mono">{cat.index_file}</span> {#if cat.index_file_exists}<span class="badge ok">present</span>{:else}<span class="badge warn">missing</span>{/if}{:else}not configured (set a cache directory; pitv_content writes it under index/){/if}
      {#if li?.finished_at} · last import {fmtDateTime(li.finished_at)}{/if}</div>
    {#if details.length}<details class="mt"><summary class="small">Import details ({details.length})</summary><pre class="log">{details.join('\n')}</pre></details>{/if}
  {:else}<div class="skeleton" style="height:40px"></div>{/if}
  <div class="mt"><JobList kind="catalogue" limit={3} /></div>
</div>
