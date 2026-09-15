<script>
  // One settings field of either application, drawn for its type. The field comes from a
  // settings schema: {key, label, help, type, default, choices?, min?, max?, step?, options?,
  // restart_required?}. Composite types (weights, dayparts, music blocks) use their own editors.
  import ChipList from './ChipList.svelte';
  import DecadePicker from './DecadePicker.svelte';
  import OrderedChoices from './OrderedChoices.svelte';
  import WeightRows from '../pages/admin/WeightRows.svelte';
  import DaypartTable from '../pages/admin/DaypartTable.svelte';
  import MusicBlocks from '../pages/admin/MusicBlocks.svelte';
  import { fmtProfile } from '../lib/format.js';

  let { field: f, value = $bindable(), error = '', changed = false, secretSet = false } = $props();
  const WIDE = new Set(['list', 'chips', 'path', 'weights', 'times', 'dayparts', 'music_blocks', 'decades', 'readonly']);
  const placeholder = (f) => (f.type === 'time' ? 'HH:MM' : f.type === 'hours' ? 'e.g. 1,2,3 or 01:00-06:00' : f.type === 'path' ? '/path/to/folder'
    : f.default != null && f.default !== '' && typeof f.default !== 'object' ? `default: ${f.default}` : '');
</script>

{#snippet title()}
  {f.label ?? f.key}{#if changed}<span class="badge info">changed</span>{/if}{#if f.restart_required}<span class="badge" title="Takes effect after a restart">restart</span>{/if}
{/snippet}
{#snippet notes()}
  {#if f.type === 'secret' && secretSet}<span class="help">Currently set; only sent if you type a new value.</span>{/if}
  {#if f.help}<span class="help">{f.help}</span>{/if}
  {#if error}<span class="help err">{error}</span>{/if}
{/snippet}

{#if f.type === 'bool'}
  <label class="check" class:changed><input type="checkbox" bind:checked={value} /> {@render title()}{@render notes()}</label>
{:else if f.type === 'dayparts' || f.type === 'music_blocks' || f.type === 'weights' || f.type === 'times'}
  <div class="field wide" class:changed>
    <span>{@render title()}</span>
    {#if f.type === 'dayparts'}<DaypartTable bind:rows={value} />
    {:else if f.type === 'music_blocks'}<MusicBlocks bind:value />
    {:else}<WeightRows {value} onchange={(v) => (value = v)} type={f.type === 'times' ? 'time' : 'number'} {...f.options ?? {}} />{/if}
    {@render notes()}
  </div>
{:else}
  <label class="field" class:wide={WIDE.has(f.type)} class:changed>
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
      <span class="small">{f.key === 'content_profile' ? fmtProfile(value) : JSON.stringify(value)}</span>
    {:else if f.type === 'secret'}
      <input type="password" autocomplete="new-password" bind:value placeholder={secretSet ? 'set: leave blank to keep' : 'not set'} />
    {:else}
      <input type="text" class:mono={f.type === 'path' || f.type === 'hours'} bind:value placeholder={placeholder(f)} />
    {/if}
    {@render notes()}
  </label>
{/if}

<style>
  .changed > span:first-child, label.check.changed { font-weight: 650; }
  .field > span :global(.badge) { margin-left: .4rem; }
  .pair { display: grid; gap: .4rem; grid-template-columns: 1fr 1fr; }
  .pair > span { display: flex; flex-direction: column; font-size: .85rem; }
</style>
