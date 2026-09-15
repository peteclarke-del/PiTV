<script>
  import AppBadge from '../../components/AppBadge.svelte';
  import { untrack } from 'svelte';
  import { get, post, put, del, tryApi, confirmApi } from '../../lib/api.js';
  import { changes, noteChange, toast } from '../../lib/stores.svelte.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { onEnter, downloadJson } from '../../lib/util.js';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
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
  const summarise = (r) => Object.entries(r ?? {}).map(([k, v]) => `${v} ${k.replace(/_/g, ' ')}`).join(', ');
  const generate = guard(async () => {
    const r = await tryApi(post('/api/lineup/generate', { rebalance: false }), { success: 'Line-ups generated' });
    if (r) { toast.info(summarise(r) || 'Nothing was unassigned'); load(); loadCounts(); noteChange('library'); }
  });
  const rebalanceLineups = guard(async () => {
    const r = await confirmApi('Redistribute every unpinned series and film across the channels? Pinned entries stay where they are.',
      { title: 'Rebalance line-ups', okLabel: 'Rebalance', danger: true }, () => post('/api/lineup/generate', { rebalance: true }), { success: 'Line-ups rebalanced' });
    if (r) { toast.info(summarise(r)); load(); loadCounts(); noteChange('library'); }
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
    if (r) { toast.success(`Imported ${r.entries ?? 0} entries${r.unknown_channels ? `; ${r.unknown_channels} unknown channel(s) skipped` : ''}`); load(); loadCounts(); noteChange('library'); }
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
  <div class="card pad-0 table-wrap">
    <table>
      <thead><tr><th>On</th><th>Channel</th><th>Content</th><th>Short</th><th>Pattern</th><th>Ads</th><th class="num">Line-up</th><th>Description</th><th></th></tr></thead>
      <tbody>
        {#each channels ?? [] as c (c.id)}
          <tr class="clickable" class:off={!c.enabled} tabindex="0" onclick={() => (editing = c)} onkeydown={onEnter(() => (editing = c))}>
            <td onclick={(e) => e.stopPropagation()}><label class="switch" title={c.enabled ? 'Enabled: click to disable' : 'Disabled: click to enable'}><input type="checkbox" checked={!!c.enabled} onchange={(e) => toggle(c, e.currentTarget.checked)} /><span></span></label></td>
            <td><ChannelBadge channel={c} /></td>
            <td>{#if c.content && c.content !== 'general'}<span class="badge info">{c.content}</span>{:else}<span class="muted small">general</span>{/if}</td>
            <td>{c.short_name}</td>
            <td class="mono small">{c.pattern}</td>
            <td class="small nowrap">{c.ads_enabled ? `${c.ads_per_break} per break` : 'off'}{#if c.family_safe_ads}<span class="badge ok" title="Family-safe adverts only">🛡 family</span>{/if}</td>
            <td class="num" onclick={(e) => e.stopPropagation()}>{#if ['general', 'cartoons'].includes(c.content ?? 'general')}<button class="small" onclick={() => (lineupFor = c)}>{counts[c.id] ?? 0} · Line-up</button>{:else}<span class="muted">–</span>{/if}</td>
            <td class="small muted" style="max-width:280px"><div class="truncate">{c.description}</div></td>
            <td class="right nowrap"><button class="small danger" onclick={(e) => { e.stopPropagation(); remove(c); }}>Delete</button></td>
          </tr>
        {:else}
          <tr><td colspan="9" class="empty">{channels ? 'No channels.' : 'Loading…'}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  <p class="tiny muted">Toggle a channel on or off with the switch; reorder by editing numbers. Enabling, disabling and other channel changes apply on the next schedule build (Dashboard: Build schedule or Rebuild week).</p>
</div>

<style>
  tr.off td { opacity: .6; }
  tr.off td:first-child { opacity: 1; }
  .switch { position: relative; display: inline-block; width: 38px; height: 22px; cursor: pointer; }
  .switch input { opacity: 0; width: 0; height: 0; position: absolute; }
  .switch span { position: absolute; inset: 0; border-radius: 11px; background: var(--border-strong); transition: background .15s; }
  .switch span::before { content: ''; position: absolute; width: 16px; height: 16px; left: 3px; top: 3px; border-radius: 50%; background: #fff; transition: transform .15s; }
  .switch input:checked + span { background: var(--ok); }
  .switch input:checked + span::before { transform: translateX(16px); }
  .switch input:focus-visible + span { outline: 2px solid var(--info); }
</style>

{#if editing}
  <ChannelEditor channel={editing} onclose={() => (editing = null)} onsaved={() => { editing = null; load(); }} />
{/if}
{#if lineupFor}
  <LineupDrawer channel={lineupFor} channels={channels ?? []} onclose={() => (lineupFor = null)} onchanged={loadCounts} />
{/if}
