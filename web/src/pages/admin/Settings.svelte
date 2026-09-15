<script>
  // PiTV's settings, one pane at a time, drawn from GET /api/settings/schema. Which fields show
  // follows the familiarity level; edits in several panes are saved together.
  import { onMount } from 'svelte';
  import { get, put, tryApi } from '../../lib/api.js';
  import { buildSchedule } from '../../lib/actions.js';
  import { route, toast } from '../../lib/stores.svelte.js';
  import { navigate } from '../../lib/router.js';
  import { shown } from '../../lib/prefs.svelte.js';
  import { guard } from '../../lib/guard.svelte.js';
  import SchemaForm from '../../components/SchemaForm.svelte';
  import AppBadge from '../../components/AppBadge.svelte';
  import Tabs from '../../components/Tabs.svelte';

  let doc = $state(null);
  let savedOnce = $state(false);
  const load = async () => { doc = (await tryApi(get('/api/settings/schema'))) ?? doc; };
  onMount(load);

  // Panes with nothing to show at this level drop out of the pane list.
  let panes = $derived((doc?.panes ?? []).filter((p) => doc.fields.some((f) => f.pane === p.id && shown(f.level))));
  let pane = $derived(panes.find((p) => p.id === route.parts[2]) ?? panes[0]);

  const save = guard(async (body) => {
    if (await tryApi(put('/api/settings', body), { success: 'Settings saved' })) { await load(); savedOnce = true; }
  });
  const build = guard(() => buildSchedule().then((r) => { if (r) toast.info('Changes apply to newly built days; use Rebuild week on the dashboard to redo existing days.'); }));
</script>

{#if doc}
  <div class="stack">
    <div class="row">
      <p class="scope" style="margin:0;flex:1">Every setting here is <AppBadge app="pitv" />'s: it shapes the schedule, playback and what PiTV asks pitv_content for. pitv_content's own settings are under Content, Settings.</p>
      <button onclick={build} disabled={build.busy}>Build schedule</button>
    </div>
    {#if savedOnce}<div class="note">Saved. Schedule settings apply to days built from now on: use Build schedule for new days, or Rebuild week on the dashboard to redo the existing ones.</div>{/if}
    <!-- `panes` is already narrowed to the level, by the fields each pane holds. -->
    <Tabs tabs={panes.map((p) => ({ id: p.id, label: p.label, title: p.help, level: 'basic' }))} active={pane?.id} onselect={(id) => navigate(`/admin/settings/${id}`)} label="Settings panes" />
    {#if pane}
      <SchemaForm schema={doc.fields} group={pane.label} intro={pane.help} app="pitv" onsave={save} saving={save.busy} />
    {/if}
  </div>
{:else}
  <div class="skeleton" style="height:300px"></div>
{/if}
