<script>
  // Schema-driven settings form for pitv_content's PUT /api/settings. Each field is
  // {key, label, help, type, default, value, choices?, min?, max?, group, restart_required};
  // only changed keys are submitted, and secrets are never echoed back (blank = keep).
  import ChipList from './ChipList.svelte';
  import { num } from '../lib/util.js';

  let { schema = [], onsave, saving = false, errors = {}, groups = ['Providers', 'Search', 'Encoding', 'Schedule', 'Advanced'] } = $props();
  const SECRET_MASK = '••••';
  let values = $state({});
  // Re-seed the editable copy only when a new schema arrives, not on every keystroke.
  let seeded = null;
  $effect(() => {
    if (schema === seeded) return;
    seeded = schema;
    const v = {};
    for (const f of schema) v[f.key] = original(f);
    values = v;
  });
  const clone = (x) => (Array.isArray(x) ? [...x] : x);
  const blank = (t) => (t === 'bool' ? false : t === 'list' ? [] : t === 'int' || t === 'float' ? 0 : '');
  const same = (a, b) => JSON.stringify(a ?? null) === JSON.stringify(b ?? null);
  const original = (f) => (f.type === 'secret' ? '' : clone(f.value ?? f.default ?? blank(f.type)));
  const isChanged = (f) => !same(values[f.key], original(f));
  let changedKeys = $derived(schema.filter(isChanged).map((f) => f.key));
  let restart = $derived(schema.some((f) => f.restart_required && isChanged(f)));
  let grouped = $derived.by(() => {
    const order = [...groups, ...new Set(schema.map((f) => f.group || 'Other').filter((g) => !groups.includes(g)))];
    return order.map((g) => [g, schema.filter((f) => (f.group || 'Other') === g)]).filter(([, fs]) => fs.length);
  });
  const secretSet = (f) => f.value === SECRET_MASK || (typeof f.value === 'string' && f.value.length > 0);

  function coerce(f, v) {
    if (f.type === 'int' || f.type === 'float') return num(v, { min: f.min ?? -Infinity, max: f.max ?? Infinity, int: f.type === 'int' });
    if (f.type === 'hours') return String(v ?? '').trim();
    return v;
  }
  function submit() {
    const body = {};
    for (const f of schema) if (isChanged(f)) body[f.key] = coerce(f, values[f.key]);
    onsave?.(body);
  }
  function resetDefaults() {
    for (const f of schema) values[f.key] = f.type === 'secret' ? '' : clone(f.default ?? blank(f.type));
  }
  const placeholder = (f) => (f.type === 'time' ? 'HH:MM' : f.type === 'hours' ? 'e.g. 1,2,3 or 01:00-06:00' : f.type === 'path' ? '/path/to/folder' : f.default != null && f.default !== '' ? `default: ${f.default}` : '');
</script>

{#if !schema.length}
  <p class="muted small">No settings reported.</p>
{:else}
  <div class="stack">
    {#each grouped as [group, fields] (group)}
      <div class="card">
        <div class="card-title"><h3>{group}</h3></div>
        <div class="form-grid">
          {#each fields as f (f.key)}
            {@const err = errors[f.key]}
            {#if f.type === 'bool'}
              <label class="check" class:changed={isChanged(f)}><input type="checkbox" bind:checked={values[f.key]} /> {f.label ?? f.key}{#if isChanged(f)}<span class="badge info">changed</span>{/if}{#if f.restart_required}<span class="badge" title="Restart required after change">restart</span>{/if}
                {#if f.help}<span class="help">{f.help}</span>{/if}{#if err}<span class="help err">{err}</span>{/if}</label>
            {:else}
              <label class="field" class:wide={f.type === 'list' || f.type === 'path'} class:changed={isChanged(f)}>
                <span>{f.label ?? f.key}{#if isChanged(f)}<span class="badge info">changed</span>{/if}{#if f.restart_required}<span class="badge" title="Restart required after change">restart</span>{/if}</span>
                {#if f.type === 'choice'}
                  <select bind:value={values[f.key]}>{#each f.choices ?? [] as c (String(typeof c === 'object' ? c.value : c))}{@const cv = typeof c === 'object' ? c.value : c}<option value={cv}>{typeof c === 'object' ? c.label ?? c.value : c}</option>{/each}</select>
                {:else if f.type === 'int' || f.type === 'float'}
                  <input type="number" step={f.type === 'float' ? 'any' : '1'} min={f.min ?? undefined} max={f.max ?? undefined} bind:value={values[f.key]} placeholder={placeholder(f)} />
                {:else if f.type === 'list'}
                  <ChipList value={values[f.key] ?? []} onchange={(v) => (values[f.key] = v)} placeholder="add…" label={f.label ?? f.key} />
                {:else if f.type === 'secret'}
                  <input type="password" autocomplete="new-password" bind:value={values[f.key]} placeholder={secretSet(f) ? 'set: leave blank to keep' : 'not set'} />
                {:else}
                  <input type="text" class:mono={f.type === 'path' || f.type === 'time' || f.type === 'hours'} bind:value={values[f.key]} placeholder={placeholder(f)} />
                {/if}
                {#if f.type === 'secret' && secretSet(f)}<span class="help">Currently set; only sent if you type a new value.</span>{/if}
                {#if f.help}<span class="help">{f.help}</span>{/if}
                {#if err}<span class="help err">{err}</span>{/if}
              </label>
            {/if}
          {/each}
        </div>
      </div>
    {/each}
    {#if restart}<div class="warn-box">A changed setting requires pitv_content to be restarted before it takes effect.</div>{/if}
    <div class="row">
      <button class="primary" onclick={submit} disabled={saving || !changedKeys.length}>Save{changedKeys.length ? ` (${changedKeys.length})` : ''}</button>
      <button onclick={resetDefaults} disabled={saving}>Reset to defaults</button>
      {#if changedKeys.length}<span class="small muted">Unsaved: {changedKeys.join(', ')}</span>{/if}
    </div>
  </div>
{/if}

<style>
  .changed > span:first-child, label.check.changed { font-weight: 650; }
  .help.err { color: var(--danger); font-weight: 550; }
  .field > span .badge { margin-left: .4rem; }
</style>
