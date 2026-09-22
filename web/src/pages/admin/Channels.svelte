<script>
  import AppBadge from '../../components/AppBadge.svelte';
  import { untrack } from 'svelte';
  import { get, post, put, del, tryApi, confirmApi } from '../../lib/api.js';
  import { changes, noteChange, toast } from '../../lib/stores.svelte.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { hasAds, hasLineup } from '../../lib/format.js';
  import { downloadJson } from '../../lib/util.js';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
  import DataTable from '../../components/DataTable.svelte';
  import ChannelEditor from './ChannelEditor.svelte';
  import LineupDrawer from './LineupDrawer.svelte';

  let channels = $state(null);
  let editing = $state(null); // channel object or {} for new
  let lineupFor = $state(null);
  let counts = $state({});     // channel_id -> line-up entry count
  let fileInput = $state(null);
  async function loadCounts() {
    const all = (await tryApi(get('/api/lineup'))) ?? [];
    const m = {};
    for (const e of all) m[e.channel_id] = (m[e.channel_id] ?? 0) + 1;
    counts = m;
  }
  // Line-ups decide home channels, which the catalogue shows too: bumping `library` refetches
  // this page (through the effect below) and anything else that lists them.
  const summarise = (r) => Object.entries(r ?? {}).map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`).join(', ');
  const generate = guard(async () => {
    const r = await tryApi(post('/api/lineup/generate', { rebalance: false }), { success: 'Line-ups generated' });
    if (r) { toast.info(summarise(r) || 'Nothing was unassigned'); noteChange('library'); }
  });
  const rebalanceLineups = guard(async () => {
    const r = await confirmApi('Redistribute every unpinned series and film across the channels? Pinned entries stay where they are.',
      { title: 'Rebalance line-ups', okLabel: 'Rebalance', danger: true }, () => post('/api/lineup/generate', { rebalance: true }), { success: 'Line-ups rebalanced' });
    if (r) { toast.info(summarise(r)); noteChange('library'); }
  });
  const exportLineup = guard(async () => {
    const doc = await tryApi(get('/api/lineup/export'));
    if (doc) downloadJson(doc, `pitv-lineup-${new Date().toISOString().slice(0, 10)}.json`);
  });
  async function importLineup(e) {
    const file = e.currentTarget.files?.[0]; e.currentTarget.value = '';
    if (!file) return;
    let doc;
    try { doc = JSON.parse(await file.text()); } catch { toast.error('That file is not JSON'); return; }
    const r = await confirmApi(`Apply the line-up from "${file.name}"? Channels are matched by number; titles not in the library become external entries.`,
      { title: 'Import line-up', okLabel: 'Import' }, () => post('/api/lineup/import', doc));
    if (r) { toast.success(`Imported ${r.entries ?? 0} entries${r.unknown_channels ? `; ${r.unknown_channels} unknown channel(s) skipped` : ''}`); noteChange('library'); }
  }

  async function load() {
    channels = (await tryApi(get('/api/channels'))) ?? channels ?? [];
  }
  $effect(() => { changes.library; untrack(() => { load(); loadCounts(); }); });

  async function remove(c) {
    const r = await confirmApi(`Delete channel ${c.number} "${c.name}"? Its schedule is removed and ${c.show_count} shows lose their home channel.`,
      { title: 'Delete channel', okLabel: 'Delete', danger: true }, () => del(`/api/channels/${c.id}`), { success: 'Channel deleted' });
    if (r) { load(); noteChange('schedule'); }
  }
  async function toggle(c, enabled) {
    const r = await tryApi(put(`/api/channels/${c.id}`, { enabled }), { success: `${c.name} ${enabled ? 'enabled' : 'disabled'}; rebuild the schedule to apply` });
    if (r) { load(); noteChange('schedule'); }
  }
  const stop = (e) => e.stopPropagation();   // controls inside a row that opens the editor
  const columns = [
    { key: 'enabled', label: 'On', class: 'control', get: (c) => !!c.enabled, cell: switchCell },
    { key: 'number', label: 'Channel', cell: channelCell },
    { key: 'content', label: 'Content', get: (c) => c.content || 'general', cell: contentCell },
    { key: 'short_name', label: 'Short' },
    { key: 'pattern', label: 'Pattern', class: 'mono small' },
    { key: 'ads', label: 'Ads', class: 'small nowrap', get: (c) => (hasAds(c) ? c.ads_per_break : 0), cell: adsCell },
    { key: 'lineup', label: 'Line-up', class: 'num', get: (c) => (hasLineup(c) ? counts[c.id] ?? 0 : null), cell: lineupCell },
    { key: 'description', label: 'Description', class: 'small muted', cell: descriptionCell },
    { key: 'actions', label: '', class: 'right nowrap', sortable: false, cell: actionsCell },
  ];
</script>

<div class="stack">
  <div class="row" style="gap:.5rem"><h2 style="margin:0">Channels and line-ups</h2><AppBadge app="pitv" /></div>
  <p class="scope" style="margin:-.4rem 0 0">What each channel carries and how PiTV schedules it. Changes apply on the next schedule build.</p>
  <div class="row">
    <button class="primary" onclick={() => (editing = {})}>Add channel</button>
    <span class="spacer"></span>
    <button onclick={generate} disabled={generate.busy || !channels?.length} title="Give every series and film without a channel a place in a line-up">Generate line-ups</button>
    <button onclick={rebalanceLineups} disabled={rebalanceLineups.busy || !channels?.length} title="Redistribute unpinned entries across channels">Rebalance</button>
    <button onclick={exportLineup} disabled={exportLineup.busy}>Export</button>
    <button onclick={() => fileInput?.click()}>Import…</button>
    <input type="file" accept="application/json,.json" bind:this={fileInput} onchange={importLineup} hidden />
  </div>
  <p class="tiny muted" style="margin:-.5rem 0 0">Line-ups decide which series and films each channel carries. Pinned entries survive rebalance; a series or film can be on one channel only.</p>
  <DataTable id="channels-list" {columns} rows={channels} sort={{ key: 'number', dir: 'asc' }} empty="No channels."
    onrow={(c) => (editing = c)} rowClass={(c) => (c.enabled ? '' : 'off')} />
  <p class="tiny muted">Toggle a channel on or off with the switch; reorder by editing numbers. Enabling, disabling and other channel changes apply on the next schedule build (Dashboard: Build schedule or Rebuild week).</p>
</div>

<style>
  /* A disabled channel's row is dimmed, but its switch is how it gets enabled again. */
  .switch { appearance: none; position: relative; width: 38px; height: 22px; min-height: 0; margin: 0; padding: 0; border: 0; border-radius: 11px;
    background: var(--border-strong); cursor: pointer; vertical-align: middle; transition: background .15s; }
  .switch::before { content: ''; position: absolute; width: 16px; height: 16px; left: 3px; top: 3px; border-radius: 50%; background: #fff; transition: transform .15s; }
  .switch:checked { background: var(--ok); }
  .switch:checked::before { transform: translateX(16px); }
  .switch:focus { outline: none; }
  .switch:focus-visible { outline: 2px solid var(--info); }
</style>

{#snippet switchCell(c)}<input type="checkbox" role="switch" class="switch" aria-label="Enabled" title={c.enabled ? 'Enabled: click to disable' : 'Disabled: click to enable'}
  checked={!!c.enabled} onclick={stop} onchange={(e) => toggle(c, e.currentTarget.checked)} />{/snippet}
{#snippet channelCell(c)}<ChannelBadge channel={c} />{/snippet}
{#snippet contentCell(c)}{#if c.content && c.content !== 'general'}<span class="badge info">{c.content}</span>{:else}<span class="muted small">general</span>{/if}{/snippet}
{#snippet adsCell(c)}{hasAds(c) ? `up to ${c.ads_per_break} per break` : 'none'}{#if c.family_safe_ads}<span class="badge ok" title="Family-safe adverts only">🛡 family</span>{/if}{/snippet}
{#snippet lineupCell(c)}{#if hasLineup(c)}<button class="small" onclick={(e) => { stop(e); lineupFor = c; }}>{counts[c.id] ?? 0} · Line-up</button>{:else}<span class="muted">–</span>{/if}{/snippet}
{#snippet descriptionCell(c)}<div class="truncate" style="max-width:280px">{c.description}</div>{/snippet}
{#snippet actionsCell(c)}<button class="small danger" onclick={(e) => { stop(e); remove(c); }}>Delete</button>{/snippet}

{#if editing}
  <ChannelEditor channel={editing} onclose={() => (editing = null)} onsaved={() => { editing = null; load(); }} />
{/if}
{#if lineupFor}
  <LineupDrawer channel={lineupFor} channels={channels ?? []} onclose={() => (lineupFor = null)} onchanged={loadCounts} />
{/if}
