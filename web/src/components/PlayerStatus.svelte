<script>
  import { player } from '../lib/stores.svelte.js';
  let s = $derived(player.state);
</script>

<div class="ps small">
  <span class="dot" class:on={s.online}></span>
  {#if s.online}
    {#if s.channel}<span><b>Ch {s.channel.number}</b> {s.channel.name ?? ''}</span>{:else}<span>Player online</span>{/if}
    {#if s.testcard}<span class="badge">Test card</span>{/if}
    {#if s.slot?.title}<span class="sep">·</span><span class="truncate">{s.slot.title}{s.slot.subtitle ? `: ${s.slot.subtitle}` : ''}</span>{/if}
    {#if s.paused}<span class="badge warn">Paused</span>{/if}
    {#if s.behind_live}<span class="badge info">Behind live</span>{/if}
    {#if s.muted}<span class="badge">Muted</span>{:else if s.volume !== null}<span class="sep">·</span><span>Vol {s.volume}</span>{/if}
    {#if s.hwdec}<span class="badge ok" title="Hardware decoder in use">{s.hwdec}</span>{/if}
    {#if s.error}<span class="badge danger" title={s.error}>error</span>{/if}
  {:else}
    <span class="muted">Player offline</span>
  {/if}
</div>

<style>
  .ps { display: flex; align-items: center; gap: .4rem; flex-wrap: wrap; color: var(--fg-muted); min-width: 0; }
  .dot { width: .6rem; height: .6rem; border-radius: 50%; background: var(--fg-faint); flex: none; }
  .dot.on { background: var(--ok); box-shadow: 0 0 0 3px color-mix(in srgb, var(--ok) 25%, transparent); }
  .truncate { max-width: 260px; }
</style>
