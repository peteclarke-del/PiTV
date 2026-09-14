<script>
  import { player } from '../lib/stores.svelte.js';
  let s = $derived(player.state);
</script>

<div class="ps small">
  <span class="dot" class:on={s.online}></span>
  {#if s.online}
    <span>Player online</span>
    {#if s.channel?.number}<span class="sep">·</span><span>Ch {s.channel.number}{s.channel.name ? ` ${s.channel.name}` : ''}</span>{/if}
    {#if s.title}<span class="sep">·</span><span class="truncate">{s.title}</span>{/if}
    {#if s.paused}<span class="badge warn">Paused</span>{/if}
    {#if s.muted}<span class="badge">Muted</span>{:else if s.volume !== null && s.volume !== undefined}<span class="sep">·</span><span>Vol {s.volume}</span>{/if}
  {:else}
    <span class="muted">Player offline</span>
  {/if}
</div>

<style>
  .ps { display: flex; align-items: center; gap: .4rem; flex-wrap: wrap; color: var(--fg-muted); }
  .dot { width: .6rem; height: .6rem; border-radius: 50%; background: var(--fg-faint); flex: none; }
  .dot.on { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 25%, transparent); }
  .sep { color: var(--fg-faint); }
</style>
