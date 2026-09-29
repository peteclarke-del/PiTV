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
  // Within a group, fields are drawn under their section heading, in the order the schema lists
  // them. A schema with no sections (pitv_content's) comes back as one unheaded block, so the
  // form looks the same as it did before sections existed.
  const sections = (fields) => {
    const names = [...new Set(fields.map((f) => f.section || ''))];
    return names.map((s) => [s, fields.filter((f) => (f.section || '') === s)]);
  };
  // A section with a table or a long run of fields takes the whole width and lays its fields
  // out in columns; the rest are panels packed side by side. A lone section has nothing to
  // sit beside, so it takes the width too.
  const COMPOSITE = new Set(['dayparts', 'weights', 'times']);
  const LONG_SECTION = 6;
  const wide = (fs) => fs.length > LONG_SECTION || fs.some((x) => COMPOSITE.has(x.type));
  // Columns cannot flow past a panel that spans them, so a narrow panel caught alone between
  // two wide ones would sit beside an empty column: it takes the width as well.
  const laidOut = (fields) => {
    const parts = sections(fields).map(([name, fs]) => ({ name, fs, span: wide(fs) }));
    parts.forEach((p, i) => {
      const alone = (parts[i - 1]?.span ?? true) && (parts[i + 1]?.span ?? true);
      if (alone) p.span = true;
    });
    return parts;
  };
  const plain = (fs) => fs.filter((x) => !COMPOSITE.has(x.type));
  const editors = (fs) => fs.filter((x) => COMPOSITE.has(x.type));
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
      {@const parts = laidOut(fields)}
      <section class="pane">
        <div class="card-title"><h3>{name}</h3>{#if app}<AppBadge {app} />{/if}</div>
        {#if intro && group}<p class="scope">{intro}</p>{/if}
        <div class="masonry panels">
          {#each parts as { name: section, fs, span } (section)}
            <div class="card panel" class:span>
              {#if section}<h4 class="section">{section}</h4>{/if}
              <!-- Plain fields first, then the editors with rows of their own, which need more width. -->
              {#each [['fields', plain(fs)], ['editors', editors(fs)]] as [kind, list] (kind)}
                {#if list.length}
                  <div class={kind}>
                    {#each list as f (f.key)}
                      <!-- The editable copy is seeded just after a new schema renders; bind only once it holds the key. -->
                      {#if f.key in values}
                        <SettingField field={f} bind:value={values[f.key]} error={errors[f.key]} changed={isChanged(f)} secretSet={secretSet(f)} />
                      {/if}
                    {/each}
                  </div>
                {/if}
              {/each}
            </div>
          {/each}
        </div>
      </section>
    {:else}
      <p class="muted small">Nothing here at this level.</p>
    {/each}
    {#if hidden && nextLevel}<p class="small muted">{hidden} more setting{hidden === 1 ? '' : 's'} here at {nextLevel}{nextLevel === 'Standard' ? ' and Advanced' : ''}.</p>{/if}
    {#if restart}<div class="warn-box">A changed setting takes effect after pitv_content is restarted.</div>{/if}
    <div class="row savebar">
      <button class="primary" onclick={submit} disabled={saving || !changed.length}>Save{changed.length ? ` (${changed.length})` : ''}</button>
      <button onclick={resetShown} disabled={saving} title="Puts the defaults back into the fields shown; nothing is saved until you press Save">Defaults for these</button>
      {#if changed.length}<span class="small muted">Unsaved: {changed.map((f) => f.label ?? f.key).join(', ')}</span>{/if}
    </div>
  </div>
{/if}

<style>
  .pane .card-title { margin-bottom: .3rem; }
  .panels { columns: 36rem; }
  .panel { container: panel / inline-size; }
  .section {
    margin: 0 0 .7rem; font-size: .82rem; font-weight: 650; letter-spacing: .04em;
    text-transform: uppercase; color: var(--fg-muted);
  }
  /* One column of field rows in a panel, two or three in a section that has the whole width. */
  .fields { display: grid; align-items: start; gap: 1rem 2.5rem; grid-template-columns: repeat(auto-fill, minmax(min(100%, 32rem), 1fr)); }
  .editors { display: grid; align-items: start; gap: 1.2rem 1rem; grid-template-columns: repeat(auto-fit, minmax(min(100%, 46rem), 1fr)); }
  .fields + .editors { margin-top: 1.1rem; }
  /* Saving stays in reach however long the pane is. */
  .savebar { position: sticky; bottom: 0; z-index: 2; padding: .6rem 0; background: var(--bg); border-top: 1px solid var(--border); }
</style>
