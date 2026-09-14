<script>
  import { untrack } from 'svelte';
  import { get, post, tryApi } from '../../lib/api.js';
  import { changes, confirm, player } from '../../lib/stores.svelte.js';
  import { fmtDateTime, fmtDuration } from '../../lib/format.js';
  import Remote from '../../components/Remote.svelte';
  import PlayerStatus from '../../components/PlayerStatus.svelte';

  let channels = $state([]);
  let history = $state([]);
  async function load() {
    try {
      const [c, h] = await Promise.all([get('/api/channels'), get('/api/history', { limit: 50 })]);
      channels = c; history = h;
    } catch { /* ignore */ }
  }
  $effect(() => { changes.player; untrack(load); }); // eslint-disable-line no-unused-expressions

  async function restart() {
    if (await confirm('Restart the player service? The picture will drop for a few seconds.', { title: 'Restart player', okLabel: 'Restart' }))
      tryApi(post('/api/system/service/pitv-player/restart'), { success: 'Restart requested' });
  }
  const hidden = ['online', 'ok', 'offline', 'channel', 'paused', 'muted', 'volume', 'title', 'error'];
  let extra = $derived(Object.entries(player.state).filter(([k, v]) => !hidden.includes(k) && v !== null && typeof v !== 'object'));
</script>

<div class="two">
  <div class="stack">
    <div class="card">
      <div class="card-title"><h3>Player</h3><button class="small danger" onclick={restart}>Restart player</button></div>
      <PlayerStatus />
      {#if player.state.error}<p class="small muted mt">{player.state.error}</p>{/if}
      {#if extra.length}
        <dl class="kv mt small">{#each extra as [k, v] (k)}<dt>{k}</dt><dd>{String(v)}</dd>{/each}</dl>
      {/if}
    </div>
    <div class="card"><div class="card-title"><h3>Remote</h3></div><Remote {channels} /></div>
  </div>
  <div class="card pad-0 table-wrap">
    <table>
      <thead><tr><th>Started</th><th>Ch</th><th>Title</th><th>Length</th></tr></thead>
      <tbody>
        {#each history as h (h.id)}
          <tr><td class="nowrap small">{fmtDateTime(h.started_at)}</td><td>{h.channel_number ?? h.channel_id}</td><td>{h.title}</td>
            <td class="small muted">{h.ended_at ? fmtDuration(h.ended_at - h.started_at) : 'playing'}</td></tr>
        {:else}
          <tr><td colspan="4" class="empty">Nothing has aired yet.</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</div>

<style>
  .two { display: grid; gap: 1rem; grid-template-columns: 1fr; }
  @media (min-width: 900px) { .two { grid-template-columns: 380px 1fr; align-items: start; } }
</style>
