<script>
  // Sub-navigation within a page or drawer. Tabs above the familiarity level are left out,
  // except the active one, so following a link never lands on a missing tab.
  // tabs: [{ id, label, level?, title?, disabled? }]
  import { shown } from '../lib/prefs.svelte.js';

  let { tabs, active = $bindable(), onselect = null, label = 'Sections' } = $props();
</script>

<nav class="tabs sub" aria-label={label}>
  {#each tabs.filter((t) => shown(t.level) || t.id === active) as t (t.id)}
    <button type="button" class:active={active === t.id} aria-current={active === t.id ? 'page' : undefined} title={t.title} disabled={t.disabled}
      onclick={() => (onselect ? onselect(t.id) : (active = t.id))}>{t.label}</button>
  {/each}
</nav>
