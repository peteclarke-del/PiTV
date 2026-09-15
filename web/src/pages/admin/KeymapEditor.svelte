<script>
  import { onMount, untrack } from 'svelte';
  import { get, put, tryApi, confirmApi } from '../../lib/api.js';
  import { player, toast } from '../../lib/stores.svelte.js';
  import { ACTIONS, ACTION_LABELS, DEFAULT_KEYMAP, mergedKeymap } from '../../lib/keymap.js';
  import { onEnter } from '../../lib/util.js';

  let map = $state(null);          // {action: [keys]}
  let custom = $state(false);      // settings.keymap non-empty
  let learning = $state(null);     // action name while waiting for a key
  let baselineTs = 0;
  let learnTimer;
  let dirty = $state(false);
  let saving = $state(false);
  let manual = $state({});

  async function load() {
    const s = await tryApi(get('/api/settings'));
    if (!s) return;
    map = mergedKeymap(s.keymap);
    custom = !!s.keymap && Object.keys(s.keymap).length > 0;
    dirty = false;
  }
  onMount(() => {
    load();
    return () => clearTimeout(learnTimer);
  });

  function assign(action, key) {
    key = String(key).toUpperCase().trim();
    if (!key) return;
    for (const a of ACTIONS) map[a] = map[a].filter((k) => k !== key);
    map[action].push(key);
    dirty = true;
  }
  function addTyped(action) { assign(action, manual[action] ?? ''); manual[action] = ''; }
  function remove(action, key) { map[action] = map[action].filter((k) => k !== key); dirty = true; }
  function learn(action) {
    if (learning === action) { stopLearning(); return; }
    learning = action;
    baselineTs = player.state.last_key?.ts ?? 0;
    clearTimeout(learnTimer);
    learnTimer = setTimeout(() => { if (learning) { toast.info('No key received in 15 s. Press a button on the remote, or cancel.'); } }, 15000);
  }
  function stopLearning() { learning = null; clearTimeout(learnTimer); }
  // Only the key report is tracked; assigning it reads and writes the map, which must not re-run this.
  $effect(() => {
    const lk = player.state.last_key;
    untrack(() => {
      if (!learning || !lk?.key || (lk.ts ?? 0) <= baselineTs) return;
      assign(learning, lk.key);
      toast.success(`${lk.key} assigned to ${ACTION_LABELS[learning]}`);
      stopLearning();
    });
  });
  async function save() {
    saving = true;
    const r = await tryApi(put('/api/settings', { keymap: map }), { success: 'Keymap saved; the player picks it up on its next settings refresh' });
    saving = false;
    if (r) { custom = true; dirty = false; }
  }
  async function reset() {
    const r = await confirmApi('Forget the custom keymap and use the built-in defaults?', { title: 'Reset keymap', okLabel: 'Reset' },
      () => put('/api/settings', { keymap: {} }), { success: 'Keymap reset to defaults' });
    if (r) { map = mergedKeymap({}); custom = false; dirty = false; }
  }
  const isDefault = (a) => JSON.stringify(map[a]) === JSON.stringify(DEFAULT_KEYMAP[a]);
</script>

<div class="stack">
  <p class="small muted">Map remote buttons to actions. Click <b>Learn</b>, then press the button on the remote while the player is running: the key is added to that action and removed from any other. The OSMC RF remote (and CEC remotes and keyboards) work out of the box with the defaults.</p>
  {#if !player.state.online}<div class="note">The player is offline, so Learn cannot see key presses. You can still type key names (e.g. <code>KEY_RED</code>).</div>{/if}
  {#if player.state.last_key}<div class="tiny muted">Last key seen: <code>{player.state.last_key.key}</code>, mapped to {player.state.last_key.action ?? 'unmapped'}</div>{/if}
  {#if map}
    <div class="table-wrap">
      <table>
        <thead><tr><th>Action</th><th>Keys</th><th></th></tr></thead>
        <tbody>
          {#each ACTIONS as a (a)}
            <tr class:learning={learning === a}>
              <td class="nowrap"><b>{ACTION_LABELS[a]}</b>{#if !isDefault(a)}<span class="badge info" title="Differs from default">custom</span>{/if}</td>
              <td>
                <div class="row" style="gap:.3rem">
                  {#each map[a] as k (k)}<span class="chip mono">{k}<button onclick={() => remove(a, k)} aria-label="Remove {k}">✕</button></span>{/each}
                  <span class="inline-form">
                    <input class="narrow mono" placeholder="KEY_…" bind:value={manual[a]} onkeydown={onEnter(() => addTyped(a))} aria-label="Add key name" />
                    <button class="small ghost" onclick={() => addTyped(a)} disabled={!manual[a]}>Add</button>
                  </span>
                </div>
              </td>
              <td class="right nowrap">
                <button class="small" class:primary={learning === a} onclick={() => learn(a)} disabled={!player.state.online && learning !== a}>{learning === a ? 'Listening… cancel' : 'Learn'}</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
    <div class="row">
      <button class="primary" onclick={save} disabled={saving || !dirty}>Save keymap</button>
      <button onclick={reset} disabled={!custom && !dirty}>Reset to defaults</button>
      {#if custom}<span class="small muted">A custom keymap is active.</span>{:else}<span class="small muted">Using built-in defaults.</span>{/if}
    </div>
  {:else}
    <div class="skeleton" style="height:120px"></div>
  {/if}
</div>

<style>
  tr.learning td { background: color-mix(in srgb, var(--accent) 10%, transparent); }
</style>
