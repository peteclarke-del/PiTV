<script>
  // One settings field of either application, drawn for its type. The field comes from a
  // settings schema: {key, label, help, type, default, choices?, min?, max?, step?, options?,
  // restart_required?}. Composite types (weights, dayparts, music blocks) use their own editors.
  import ChipList from './ChipList.svelte';
  import DecadePicker from './DecadePicker.svelte';
  import OrderedChoices from './OrderedChoices.svelte';
  import WeightRows from '../pages/admin/WeightRows.svelte';
  import DaypartTable from '../pages/admin/DaypartTable.svelte';
  import { fmtProfile } from '../lib/format.js';

  let { field: f, value = $bindable(), error = '', changed = false, secretSet = false } = $props();
  const BIG = new Set(['dayparts', 'weights', 'times']);   // editors with rows of their own: the whole width
  const BROAD = new Set(['list', 'chips', 'path', 'decades', 'readonly', 'hour_list']);
  const placeholder = (f) => (f.type === 'time' ? 'HH:MM' : f.type === 'hours' ? 'e.g. 1,2,3 or 01:00-06:00' : f.type === 'path' ? '/path/to/folder'
    : f.default != null && f.default !== '' && typeof f.default !== 'object' ? `default: ${f.default}` : '');
</script>

{#snippet title()}
  {f.label ?? f.key}{#if changed}<span class="badge info">changed</span>{/if}{#if f.restart_required}<span class="badge" title="Takes effect after a restart">restart</span>{/if}
{/snippet}
{#snippet notes()}
  <span class="notes" id="help-{f.key}">
    {#if f.type === 'secret' && secretSet}<span class="help">Currently set; only sent if you type a new value.</span>{/if}
    {#if f.help}<span class="help">{f.help}</span>{/if}
    {#if error}<span class="help err">{error}</span>{/if}
  </span>
{/snippet}

{#if f.type === 'bool'}
  <div class="sf" class:changed>
    <label class="main tick"><input type="checkbox" bind:checked={value} aria-describedby="help-{f.key}" /> <span>{@render title()}</span></label>
    {@render notes()}
  </div>
{:else if BIG.has(f.type)}
  <div class="sf big" class:changed>
    <span class="main"><span>{@render title()}</span></span>
    {@render notes()}
    <div class="editor">
      {#if f.type === 'dayparts'}<DaypartTable bind:rows={value} />
      {:else}<WeightRows {value} onchange={(v) => (value = v)} type={f.type === 'times' ? 'time' : 'number'} {...f.options ?? {}} />{/if}
    </div>
  </div>
{:else}
  <div class="sf" class:broad={BROAD.has(f.type)} class:changed>
    <label class="main">
      <span>{@render title()}{#if f.type === 'slider'} <span class="mono">{Number(value ?? 0).toFixed(2)}</span>{/if}</span>
      {#if f.type === 'choice'}
        <select bind:value>{#each f.choices ?? [] as c (String(typeof c === 'object' ? c.value : c))}<option value={typeof c === 'object' ? c.value : c}>{typeof c === 'object' ? c.label ?? c.value : c}</option>{/each}</select>
      {:else if f.type === 'int' || f.type === 'float'}
        <input type="number" step={f.step ?? (f.type === 'float' ? 'any' : 1)} min={f.min} max={f.max} bind:value placeholder={placeholder(f)} />
      {:else if f.type === 'slider'}
        <input type="range" min={f.min ?? 0} max={f.max ?? 1} step={f.step ?? 0.05} bind:value />
      {:else if f.type === 'time'}
        <input type="time" bind:value />
      {:else if f.type === 'list' && f.choices?.length}
        <OrderedChoices bind:value choices={f.choices} label={f.label ?? f.key} />
      {:else if f.type === 'list' || f.type === 'chips'}
        <ChipList value={value ?? []} onchange={(v) => (value = v)} placeholder="add…" label={f.label ?? f.key} lower={!!f.options?.lower} />
      {:else if f.type === 'hour_list'}
        <ChipList value={(value ?? []).map(String)} onchange={(v) => (value = v.map((h) => parseInt(h, 10)).filter((h) => h >= 0 && h <= 23))} placeholder="hour 0–23…" label={f.label ?? f.key} />
      {:else if f.type === 'decades'}
        <DecadePicker bind:value label={f.label ?? f.key} />
      {:else if f.type === 'kind_weights'}
        <span class="pair">
          <span>TV <span class="mono">{Number(value?.tv ?? 0).toFixed(2)}</span><input type="range" min="0" max="1" step="0.05" bind:value={value.tv} aria-label="TV weight" /></span>
          <span>Film <span class="mono">{Number(value?.movie ?? 0).toFixed(2)}</span><input type="range" min="0" max="1" step="0.05" bind:value={value.movie} aria-label="Film weight" /></span>
        </span>
      {:else if f.type === 'readonly'}
        <span class="small value">{f.key === 'content_profile' ? fmtProfile(value) : JSON.stringify(value)}</span>
      {:else if value && typeof value === 'object'}
        <!-- A structured value of a type this form has no editor for. A text box showed it as
             "[object Object]" and would have saved that string over it. -->
        <code class="small value structured">{JSON.stringify(value)}</code>
      {:else if f.type === 'secret'}
        <input type="password" autocomplete="new-password" bind:value placeholder={secretSet ? 'set: leave blank to keep' : 'not set'} />
      {:else}
        <input type="text" class:mono={f.type === 'path' || f.type === 'hours'} bind:value placeholder={placeholder(f)} />
      {/if}
    </label>
    {@render notes()}
  </div>
{/if}

<style>
  /* A field is a row: its label and control on the left, what it means on the right. The help
     is the long part, and beside the control it runs across the space a wide panel has; under
     the control it wrapped into a column as narrow as the input and a dozen lines tall. In a
     panel too narrow for both (a phone, a drawer) the help goes back underneath. */
  .sf { display: grid; grid-template-columns: minmax(0, 15rem) minmax(0, 1fr); gap: .25rem 1.2rem; align-items: start; }
  .main { display: flex; flex-direction: column; gap: .25rem; font-size: .88rem; font-weight: 550; min-width: 0; }
  .main.tick { flex-direction: row; align-items: center; gap: .45rem; font-size: .92rem; font-weight: 500; cursor: pointer; }
  .main :global(input:not([type="checkbox"]):not([type="range"])), .main :global(select) { width: 100%; }
  .notes { display: flex; flex-direction: column; gap: .15rem; min-width: 0; }
  .notes:empty { display: none; }
  .help { font-weight: 400; color: var(--fg-muted); font-size: .8rem; max-width: 70ch; }
  .value { font-weight: 600; }
  .structured { font-weight: 400; overflow-wrap: anywhere; max-height: 6rem; overflow: auto; }
  /* Controls that hold a list or a path want the width themselves, so their help sits below. */
  .sf.broad, .sf.big { grid-template-columns: minmax(0, 1fr); }
  .big .help { max-width: 110ch; }
  .editor { min-width: 0; }
  .changed .main > span:first-child, .changed .main.tick { font-weight: 650; }
  .main :global(.badge) { margin-left: .4rem; }
  .pair { display: grid; gap: .4rem; grid-template-columns: 1fr 1fr; }
  .pair > span { display: flex; flex-direction: column; font-size: .85rem; }
  @container panel (max-width: 27rem) { .sf { grid-template-columns: minmax(0, 1fr); } }
</style>
