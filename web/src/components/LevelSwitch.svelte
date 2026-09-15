<script>
  // The admin's familiarity level: what the navigation, the settings panes and the editors show.
  import { LEVELS, prefs } from '../lib/prefs.svelte.js';
</script>

<div class="seg" role="radiogroup" aria-label="Show settings for">
  {#each LEVELS as [id, label, help] (id)}
    <button type="button" role="radio" aria-checked={prefs.level === id} class:on={prefs.level === id} title={help} onclick={() => (prefs.level = id)}>{label}</button>
  {/each}
</div>

<style>
  .seg { display: inline-flex; border: 1px solid var(--border); border-radius: 999px; overflow: hidden; }
  .seg button { all: unset; cursor: pointer; font-size: .78rem; font-weight: 600; padding: .25rem .75rem; color: var(--fg-muted); }
  .seg button + button { border-left: 1px solid var(--border); }
  .seg button.on { background: var(--accent); color: #fff; }
  .seg button:focus-visible { outline: 2px solid var(--info); outline-offset: -2px; }
</style>
