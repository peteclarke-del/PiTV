<script>
  // Settings form for either application, drawn from its settings schema. Fields show at the
  // chosen familiarity level (a field without a level counts as standard); `group` narrows the
  // form to one pane while edits in other panes are kept until saved. Only changed keys are
  // submitted, cleaned for their type, and secrets are never echoed back (blank keeps them).
  import AppBadge from './AppBadge.svelte';
  import SettingField from './SettingField.svelte';
  import { shown, LEVELS } from '../lib/prefs.svelte.js';
  import { blank, clean } from '../lib/settingTypes.js';

  let { schema = [], onsave, saving = false, errors = {}, groups = [], group = null, intro = '', app = '' } = $props();
  let values = $state({});
  // Re-seed the editable copy only when a new schema arrives, not on every keystroke.
  let seeded = null;
  $effect(() => {
    if (schema === seeded) return;
    seeded = schema;
    values = Object.fromEntries(schema.map((f) => [f.key, original(f)]));
  });
  const copy = (x) => (x && typeof x === 'object' ? JSON.parse(JSON.stringify(x)) : x);
  const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  const original = (f) => (f.type === 'secret' ? '' : copy(f.value ?? f.default ?? blank(f.type)));
  const isChanged = (f) => !same(values[f.key], original(f));
  // A secret comes back masked, with `is_set` saying whether one is saved; the mask is never sent back.
  const secretSet = (f) => f.is_set ?? (typeof f.value === 'string' && f.value.length > 0);

  let changed = $derived(schema.filter(isChanged));
  let restart = $derived(changed.some((f) => f.restart_required));
  let inScope = $derived(schema.filter((f) => !group || (f.group || 'Other') === group));
  let visible = $derived(inScope.filter((f) => shown(f.level)));
  let hidden = $derived(inScope.length - visible.length);
  let grouped = $derived.by(() => {
    const order = [...groups, ...new Set(visible.map((f) => f.group || 'Other').filter((g) => !groups.includes(g)))];
    return order.map((g) => [g, visible.filter((f) => (f.group || 'Other') === g)]).filter(([, fs]) => fs.length);
  });
  const nextLevel = $derived(LEVELS.find(([id]) => !shown(id))?.[1]);

  function submit() {
    onsave?.(Object.fromEntries(changed.map((f) => [f.key, clean(f, values[f.key])])));
  }
  function resetShown() {
    for (const f of visible) values[f.key] = f.type === 'secret' ? '' : copy(f.default ?? blank(f.type));
  }
</script>

{#if !schema.length}
  <p class="muted small">No settings reported.</p>
{:else}
  <div class="stack">
    {#each grouped as [name, fields] (name)}
      <div class="card">
        <div class="card-title"><h3>{name}</h3>{#if app}<AppBadge {app} />{/if}</div>
        {#if intro && group}<p class="scope">{intro}</p>{/if}
        <div class="form-grid">
          {#each fields as f (f.key)}
            <!-- The editable copy is seeded just after a new schema renders; bind only once it holds the key. -->
            {#if f.key in values}
              <SettingField field={f} bind:value={values[f.key]} error={errors[f.key]} changed={isChanged(f)} secretSet={secretSet(f)} />
            {/if}
          {/each}
        </div>
      </div>
    {:else}
      <p class="muted small">Nothing here at this level.</p>
    {/each}
    {#if hidden && nextLevel}<p class="small muted">{hidden} more setting{hidden === 1 ? '' : 's'} here at {nextLevel}{nextLevel === 'Standard' ? ' and Advanced' : ''}.</p>{/if}
    {#if restart}<div class="warn-box">A changed setting takes effect after pitv_content is restarted.</div>{/if}
    <div class="row">
      <button class="primary" onclick={submit} disabled={saving || !changed.length}>Save{changed.length ? ` (${changed.length})` : ''}</button>
      <button onclick={resetShown} disabled={saving} title="Puts the defaults back into the fields shown; nothing is saved until you press Save">Defaults for these</button>
      {#if changed.length}<span class="small muted">Unsaved: {changed.map((f) => f.label ?? f.key).join(', ')}</span>{/if}
    </div>
  </div>
{/if}
