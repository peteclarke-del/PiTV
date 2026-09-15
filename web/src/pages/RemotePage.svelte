<script>
  import { onMount } from 'svelte';
  import { get, tryApi } from '../lib/api.js';
  import Remote from '../components/Remote.svelte';
  import PlayerStatus from '../components/PlayerStatus.svelte';
  import { player } from '../lib/stores.svelte.js';

  let channels = $state([]);
  onMount(async () => {
    const d = await tryApi(get('/api/now', { next: 0 }));
    if (d) channels = d.channels.map((c) => c.channel);
  });
</script>

<div class="page">
  <div class="page-head">
    <h1>Remote</h1>
    <PlayerStatus />
  </div>
  {#if !player.state.online}
    <p class="note mb">The player is offline, so the buttons are disabled. Check the Player tab in Admin.</p>
  {/if}
  <Remote {channels} />
</div>
