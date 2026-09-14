<script>
  import { untrack } from 'svelte';
  import { get, post, del, tryApi } from '../../lib/api.js';
  import { changes, confirm } from '../../lib/stores.svelte.js';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
  import ChannelEditor from './ChannelEditor.svelte';

  let channels = $state(null);
  let editing = $state(null); // channel object or {} for new

  async function load() {
    try { channels = await get('/api/channels'); } catch { channels = channels ?? []; }
  }
  $effect(() => { changes.library; untrack(load); }); // eslint-disable-line no-unused-expressions

  async function remove(c) {
    if (!(await confirm(`Delete channel ${c.number} "${c.name}"? Its schedule is removed and ${c.show_count} shows lose their home channel.`, { title: 'Delete channel', okLabel: 'Delete', danger: true }))) return;
    if (await tryApi(del(`/api/channels/${c.id}`), { success: 'Channel deleted' })) { load(); changes.schedule++; }
  }
  async function rebalance() {
    if (!(await confirm('Clear every show\'s home channel and spread the shows evenly across the enabled channels? Manual assignments are lost.', { title: 'Rebalance shows', okLabel: 'Rebalance', danger: true }))) return;
    const r = await tryApi(post('/api/channels/rebalance'), { success: 'Shows rebalanced' });
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
      <thead><tr><th>Channel</th><th>Short</th><th>Pattern</th><th>Ads</th><th class="num">Shows</th><th>Description</th><th></th></tr></thead>
      <tbody>
        {#each channels ?? [] as c (c.id)}
          <tr class="clickable" onclick={() => (editing = c)}>
            <td><ChannelBadge channel={c} />{#if !c.enabled}<span class="badge">disabled</span>{/if}</td>
            <td>{c.short_name}</td>
            <td class="mono small">{c.pattern}</td>
            <td class="small">{c.ads_enabled ? `${c.ads_per_break} per break` : 'off'}</td>
            <td class="num">{c.show_count}</td>
            <td class="small muted" style="max-width:280px"><div class="truncate">{c.description}</div></td>
            <td class="right nowrap"><button class="small danger" onclick={(e) => { e.stopPropagation(); remove(c); }}>Delete</button></td>
          </tr>
        {:else}
          <tr><td colspan="7" class="empty">{channels ? 'No channels.' : 'Loading…'}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
  <p class="tiny muted">Reorder channels by editing their numbers. Channel changes apply on the next schedule build.</p>
</div>

{#if editing}
  <ChannelEditor channel={editing} onclose={() => (editing = null)} onsaved={() => { editing = null; load(); }} />
{/if}
