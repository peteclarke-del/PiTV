<script>
  import { toasts, toast } from '../lib/stores.svelte.js';
</script>

<div class="toasts" role="status" aria-live="polite">
  {#each toasts.list as t (t.id)}
    <button class="toast {t.kind}" onclick={() => toast.dismiss(t.id)}>{t.text}</button>
  {/each}
</div>

<style>
  .toasts {
    position: fixed; z-index: 200; left: 16px; right: 16px; bottom: 16px;
    display: flex; flex-direction: column; gap: .5rem; align-items: center; pointer-events: none;
  }
  .toast {
    pointer-events: auto; text-align: left; max-width: 480px; width: 100%;
    background: var(--bg-elev); border: 1px solid var(--border-strong); box-shadow: var(--shadow);
    border-left: 4px solid var(--info); padding: .6rem .9rem; font-weight: 500; border-radius: var(--radius-sm);
    justify-content: flex-start; white-space: pre-wrap;
  }
  .toast.success { border-left-color: var(--ok); }
  .toast.error { border-left-color: var(--danger); }
  @media (min-width: 700px) {
    .toasts { left: auto; align-items: flex-end; }
  }
</style>
