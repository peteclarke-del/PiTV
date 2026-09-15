<script>
  import { player } from '../lib/stores.svelte.js';
  import { post, tryApi } from '../lib/api.js';

  let { channels = [] } = $props();
  let online = $derived(player.state.online);
  let busy = $state(null); // channel number while a tune request is in flight

  const key = (k) => tryApi(post('/api/player/key', { key: k }));
  async function tune(n) {
    busy = n;
    await tryApi(post('/api/player/channel', { number: n }));
    busy = null;
  }
</script>

<div class="remote" class:offline={!online}>
  <div class="channels">
    {#each channels.filter((c) => c.enabled) as ch (ch.id)}
      <button class="ch" disabled={!online} onclick={() => tune(ch.number)} title={ch.name}
              style="--c:{ch.colour}" class:busy={busy === ch.number}>
        <b>{ch.number}</b><span class="truncate">{ch.short_name}</span>
      </button>
    {/each}
  </div>

  <div class="rockers">
    <div class="rocker">
      <button disabled={!online} onclick={() => key('ch_up')} aria-label="Channel up">▲</button>
      <span>CH</span>
      <button disabled={!online} onclick={() => key('ch_down')} aria-label="Channel down">▼</button>
    </div>
    <div class="middle">
      <button disabled={!online} onclick={() => key('mute')} class:active={player.state.muted}>Mute</button>
      <button disabled={!online} onclick={() => key('pause')} class:active={player.state.paused}>{player.state.paused ? 'Play' : 'Pause'}</button>
    </div>
    <div class="rocker">
      <button disabled={!online} onclick={() => key('vol_up')} aria-label="Volume up">+</button>
      <span>VOL</span>
      <button disabled={!online} onclick={() => key('vol_down')} aria-label="Volume down">−</button>
    </div>
  </div>

  <div class="dpad">
    <button class="up" disabled={!online} onclick={() => key('up')} aria-label="Up">▲</button>
    <button class="left" disabled={!online} onclick={() => key('left')} aria-label="Left">◀</button>
    <button class="ok" disabled={!online} onclick={() => key('ok')}>OK</button>
    <button class="right" disabled={!online} onclick={() => key('right')} aria-label="Right">▶</button>
    <button class="down" disabled={!online} onclick={() => key('down')} aria-label="Down">▼</button>
  </div>

  <div class="extras">
    <button disabled={!online} onclick={() => key('back')}>Back</button>
    <button disabled={!online} onclick={() => key('guide')}>Guide</button>
    <button disabled={!online} onclick={() => key('info')}>Info</button>
    <button disabled={!online} onclick={() => key('restart')} title="Restart the current programme from the beginning">↺ Restart</button>
  </div>
</div>

<style>
  .remote { display: flex; flex-direction: column; gap: 1rem; max-width: 340px; margin: 0 auto; width: 100%; }
  .remote button { min-height: 44px; font-size: 1rem; }
  .channels { display: grid; grid-template-columns: repeat(auto-fill, minmax(70px, 1fr)); gap: .5rem; }
  .ch { flex-direction: column; gap: 0; padding: .4rem .3rem; border-top: 4px solid var(--c); min-height: 56px; }
  .ch b { font-size: 1.2rem; line-height: 1.1; }
  .ch span { font-size: .72rem; color: var(--fg-muted); max-width: 100%; }
  .rockers { display: grid; grid-template-columns: 1fr 1fr 1fr; gap: .75rem; }
  .rocker { display: flex; flex-direction: column; align-items: stretch; gap: .25rem; }
  .rocker span { text-align: center; font-size: .72rem; font-weight: 700; color: var(--fg-muted); letter-spacing: .05em; }
  .middle { display: flex; flex-direction: column; gap: .5rem; justify-content: center; }
  .active { background: var(--accent); color: #fff; border-color: var(--accent); }
  .dpad { display: grid; grid-template-columns: 1fr 1fr 1fr; grid-template-rows: 1fr 1fr 1fr; gap: .4rem; width: 200px; margin: 0 auto; }
  .dpad button { aspect-ratio: 1; padding: 0; }
  .up { grid-column: 2; grid-row: 1; }
  .left { grid-column: 1; grid-row: 2; }
  .ok { grid-column: 2; grid-row: 2; border-radius: 50%; font-weight: 800; background: var(--bg-sunken); }
  .right { grid-column: 3; grid-row: 2; }
  .down { grid-column: 2; grid-row: 3; }
  .extras { display: grid; grid-template-columns: 1fr 1fr; gap: .5rem; }
  .busy { opacity: .7; }
</style>
