<script>
  import { get } from '../../lib/api.js';
  import Modal from '../../components/Modal.svelte';

  let { open = false, start = '/', onclose, onpick } = $props();
  let path = $state('/');
  let parent = $state(null);
  let dirs = $state([]);
  let error = $state('');

  async function browse(p) {
    error = '';
    try {
      const r = await get('/api/browse', { path: p });
      path = r.path; parent = r.parent; dirs = r.dirs;
    } catch (e) {
      error = e.detail || e.message;
      if (p !== '/') browse('/');
    }
  }
  $effect(() => { if (open) browse(start || '/'); });
</script>

<Modal {open} title="Choose a folder" {onclose}>
  <div class="stack">
    <div class="row">
      <button class="small" onclick={() => parent && browse(parent)} disabled={!parent}>↑ Up</button>
      <code class="truncate" style="flex:1">{path}</code>
    </div>
    {#if error}<div class="badge danger">{error}</div>{/if}
    <ul class="dirs">
      {#each dirs as d (d)}
        <li><button class="ghost" onclick={() => browse(path === '/' ? `/${d}` : `${path}/${d}`)}>📁 {d}</button></li>
      {:else}
        <li class="muted small">No sub-folders.</li>
      {/each}
    </ul>
  </div>
  {#snippet footer()}
    <button onclick={onclose}>Cancel</button>
    <button class="primary" onclick={() => onpick?.(path)}>Use this folder</button>
  {/snippet}
</Modal>

<style>
  .dirs { list-style: none; margin: 0; padding: 0; max-height: 50vh; overflow: auto; display: flex; flex-direction: column; }
  .dirs button { justify-content: flex-start; width: 100%; text-align: left; }
</style>
