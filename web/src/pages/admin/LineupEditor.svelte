<script>
  import { onMount, untrack } from 'svelte';
  import { get, put, tryApi } from '../../lib/api.js';
  import { noteChange } from '../../lib/stores.svelte.js';
  import { num } from '../../lib/util.js';
  import { guard } from '../../lib/guard.svelte.js';
  import Modal from '../../components/Modal.svelte';
  import GenrePicker from '../../components/GenrePicker.svelte';

  let { entry, channels = [], onclose, onsaved } = $props();
  const initial = untrack(() => entry);
  let facets = $state(null);
  let f = $state({
    year: initial.year ?? '', genres: [...(initial.genres ?? [])], channel_id: initial.channel_id,
    episode_minutes: initial.episode_minutes ?? '', enabled: !!initial.enabled,
    transient: !!initial.transient, next_episode: initial.next_episode ?? 1, episode_count: initial.episode_count ?? '',
  });
  onMount(async () => { facets = (await tryApi(get('/api/library/facets'))) ?? null; });

  const save = guard(async () => {
    const body = {
      year: num(f.year, { min: 1900, max: 2100, int: true }), genres: f.genres,
      channel_id: Number(f.channel_id), enabled: f.enabled, transient: f.transient,
      next_episode: num(f.next_episode, { min: 1, int: true, fallback: 1 }),
    };
    if (entry.kind === 'show') body.episode_minutes = num(f.episode_minutes, { min: 1, max: 240, int: true });
    if (entry.kind === 'show') body.episode_count = num(f.episode_count, { min: 1, int: true });
    const r = await tryApi(put(`/api/lineup/${entry.id}`, body), { success: 'Catalogue entry saved' });
    if (!r) return;
    noteChange('library');
    onsaved?.(r);
    onclose?.();
  });
</script>

<Modal open={true} title={`Edit ${entry.title}`} {onclose} width="560px">
  <div class="stack">
    <p class="scope" style="margin:0">Edit the classification PiTV uses to choose where and when this added title airs.</p>
    <div class="form-grid">
      <label class="field">Year<input type="number" min="1900" max="2100" bind:value={f.year} /></label>
      <label class="field">Channel<select bind:value={f.channel_id}>{#each channels as c (c.id)}<option value={c.id}>{c.number} {c.name}</option>{/each}</select></label>
      <div class="field wide genres">
        <span>Genres</span>
        <GenrePicker value={f.genres} options={facets?.genres ?? {}} kinds={entry.kind === 'show' ? ['episode'] : ['movie']} onchange={(v) => (f.genres = v)} label="Entry genres" empty="Choose genres" />
        <span class="help">The same programme genres offered in Channel settings.</span>
      </div>
      {#if entry.kind === 'show'}
        <label class="field">Episode length (minutes)<input type="number" min="1" max="240" bind:value={f.episode_minutes} /></label>
        <label class="field">Next episode<input type="number" min="1" bind:value={f.next_episode} /></label>
        <label class="field">Episodes in the series<input type="number" min="1" bind:value={f.episode_count} placeholder="unknown" /><span class="help">From the online match. Nothing past this is asked for.</span></label>
      {/if}
      <label class="check"><input type="checkbox" bind:checked={f.enabled} /> Included in scheduling</label>
      <label class="check"><input type="checkbox" bind:checked={f.transient} /> Remove fetched files after airing</label>
    </div>
  </div>
  {#snippet footer()}<button onclick={onclose}>Cancel</button><button class="primary" onclick={save} disabled={save.busy}>{save.busy ? 'Saving…' : 'Save'}</button>{/snippet}
</Modal>
