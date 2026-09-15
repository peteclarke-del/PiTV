<script>
  let { open = false, title = '', subtitle = '', onclose, children, footer, wide = false } = $props();
  let box = $state(null);
  $effect(() => { if (open) box?.focus(); });

  // Escape closes the layer that has focus (a confirm dialog over a drawer closes on its own), or
  // this one when nothing is focused, as after the layer above it has closed.
  function onkey(e) {
    if (e.key === 'Escape' && open && (box?.contains(e.target) || e.target === document.body)) onclose?.();
  }
</script>

<svelte:window onkeydown={onkey} />

{#if open}
  <div class="backdrop" onclick={(e) => { if (e.target === e.currentTarget) onclose?.(); }} role="presentation">
    <div class="drawer" class:wide role="dialog" aria-modal="true" aria-label={title} tabindex="-1" bind:this={box}>
      <div class="head">
        <div class="titles">
          <h2 class="truncate">{title}</h2>
          {#if subtitle}<div class="muted small truncate">{subtitle}</div>{/if}
        </div>
        <button class="ghost icon" onclick={onclose} aria-label="Close">✕</button>
      </div>
      <div class="body">{@render children?.()}</div>
      {#if footer}<div class="foot">{@render footer()}</div>{/if}
    </div>
  </div>
{/if}

<style>
  .backdrop { position: fixed; inset: 0; z-index: 90; background: rgba(0, 0, 0, .4); }
  .drawer {
    position: absolute; right: 0; top: 0; bottom: 0; width: min(100%, 520px);
    background: var(--bg-elev); border-left: 1px solid var(--border); box-shadow: var(--shadow);
    display: flex; flex-direction: column; animation: slide .18s ease-out;
  }
  .drawer.wide { width: min(100%, 720px); }
  @keyframes slide { from { transform: translateX(30px); opacity: 0; } }
  .head { display: flex; align-items: flex-start; gap: .5rem; padding: .9rem 1rem .5rem; border-bottom: 1px solid var(--border); }
  .titles { flex: 1; min-width: 0; }
  .head h2 { margin: 0; font-size: 1.1rem; }
  .body { padding: 1rem; overflow: auto; flex: 1; }
  .foot { padding: .7rem 1rem; display: flex; gap: .5rem; flex-wrap: wrap; justify-content: flex-end; border-top: 1px solid var(--border); background: var(--bg-elev); }
  @media (max-width: 600px) {
    .drawer, .drawer.wide { width: 100%; top: 6vh; border-radius: 14px 14px 0 0; border-left: 0; animation: up .2s ease-out; }
    @keyframes up { from { transform: translateY(30px); opacity: 0; } }
  }
</style>
