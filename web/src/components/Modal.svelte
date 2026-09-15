<script>
  let { open = false, title = '', onclose, children, footer, width = '460px' } = $props();
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
    <div class="modal" role="dialog" aria-modal="true" aria-label={title} style="max-width:{width}" tabindex="-1" bind:this={box}>
      <div class="head">
        <h2>{title}</h2>
        <button class="ghost icon" onclick={onclose} aria-label="Close">✕</button>
      </div>
      <div class="body">{@render children?.()}</div>
      {#if footer}<div class="foot">{@render footer()}</div>{/if}
    </div>
  </div>
{/if}

<style>
  .backdrop {
    position: fixed; inset: 0; z-index: 100; background: rgba(0, 0, 0, .45);
    display: flex; align-items: center; justify-content: center; padding: 16px;
  }
  .modal {
    background: var(--bg-elev); border: 1px solid var(--border); border-radius: var(--radius);
    box-shadow: var(--shadow); width: 100%; max-height: calc(100vh - 32px); display: flex; flex-direction: column;
  }
  .head { display: flex; align-items: center; gap: .5rem; padding: .8rem 1rem .4rem; }
  .head h2 { flex: 1; margin: 0; font-size: 1.05rem; }
  .body { padding: .4rem 1rem 1rem; overflow: auto; }
  .foot { padding: .6rem 1rem .9rem; display: flex; gap: .5rem; justify-content: flex-end; border-top: 1px solid var(--border); }
</style>
