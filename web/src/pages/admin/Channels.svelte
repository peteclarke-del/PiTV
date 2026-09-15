<script>
  import { untrack } from 'svelte';
  import { get, post, put, del, tryApi, confirmApi } from '../../lib/api.js';
  import { changes } from '../../lib/stores.svelte.js';
  import { onEnter } from '../../lib/util.js';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
  import ChannelEditor from './ChannelEditor.svelte';

  let channels = $state(null);
  let editing = $state(null); // channel object or {} for new

  async function load() {
    try { channels = await get('/api/channels'); } catch { channels = channels ?? []; }
  }
  $effect(() => { changes.library; untrack(load); });

  async function remove(c) {
    const r = await confirmApi(`Delete channel ${c.number} "${c.name}"? Its schedule is removed and ${c.show_count} shows lose their home channel.`,
      { title: 'Delete channel', okLabel: 'Delete', danger: true }, () => del(`/api/channels/${c.id}`), { success: 'Channel deleted' });
    if (r) { load(); changes.schedule++; }
  }
  async function toggle(c, enabled) {
    const r = await tryApi(put(`/api/channels/${c.id}`, { enabled }), { success: `${c.name} ${enabled ? 'enabled' : 'disabled'} – rebuild the schedule to apply` });
    if (r) { load(); changes.schedule++; }
  }
  async function rebalance() {
    const r = await confirmApi('Clear every show\'s home channel and spread the shows evenly across the enabled channels? Manual assignments are lost.',
      { title: 'Rebalance shows', okLabel: 'Rebalance', danger: true }, () => post('/api/channels/rebalance'), { success: 'Shows rebalanced' });
    if (r) channels = r;
  }
</script>

<div class="stack">
  <div class="row">
    <button class="primary" onclick={() => (editing = {})}>Add channel</button>
    <button onclick={rebalance} disabled={!channels?.length}>Rebalance shows across channels</button>
  </div>
  <div class="card pad-0 table-wrap">
    <table>
      <thead><tr><th>On</th><th>Channel</th><th>Content</th><th>Short</th><th>Pattern</th><th>Ads</th><th class="num">Shows</th><th>Description</th><th></th></tr></thead>
      <tbody>
        {#each channels ?? [] as c (c.id)}
          <tr class="clickable" class:off={!c.enabled} tabindex="0" onclick={() => (editing = c)} onkeydown={onEnter(() => (editing = c))}>
            <td onclick={(e) => e.stopPropagation()}><label class="switch" title={c.enabled ? 'Enabled – click to disable' : 'Disabled – click to enable'}><input type="checkbox" checked={!!c.enabled} onchange={(e) => toggle(c, e.currentTarget.checked)} /><span></span></label></td>
            <td><ChannelBadge channel={c} /></td>
            <td>{#if c.content && c.content !== 'general'}<span class="badge info">{c.content}</span>{:else}<span class="muted small">general</span>{/if}</td>
            <td>{c.short_name}</td>
            <td class="mono small">{c.pattern}</td>
            <td class="small nowrap">{c.ads_enabled ? `${c.ads_per_break} per break` : 'off'}{#if c.family_safe_ads}<span class="badge ok" title="Family-safe adverts only">🛡 family</span>{/if}</td>
            <td class="num">{c.show_count}</td>
            <td class="small muted" style="max-width:280px"><div class="truncate">{c.description}</div></td>
            <td class="right nowrap"><button class="small danger" onclick={(e) => { e.stopPropagation(); remove(c); }}>Delete</button></td>
          </tr>
        {:else}
          <tr><td colspan="9" class="empty">{channels ? 'No channels.' : 'Loading…'}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  <p class="tiny muted">Toggle a channel on or off with the switch; reorder by editing numbers. Enabling, disabling and other channel changes apply on the next schedule build (Dashboard → Build schedule / Rebuild week).</p>
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
