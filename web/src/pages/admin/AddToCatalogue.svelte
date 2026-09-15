<script>
  // Custom programming: a title that is not on the NAS joins the catalogue here. A series or film
  // becomes a line-up entry on a channel (chosen by its genres unless one is picked); pitv_content
  // fetches it ahead of the day it is scheduled. An advert or music video joins the wanted list.
  import { get, post, tryApi } from '../../lib/api.js';
  import { num, splitList } from '../../lib/util.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { noteChange } from '../../lib/stores.svelte.js';
  import Modal from '../../components/Modal.svelte';
  import AppBadge from '../../components/AppBadge.svelte';

  let { open = false, channels = [], onclose, onadded } = $props();
  const KINDS = [['show', 'Series'], ['movie', 'Film'], ['advert', 'Advert'], ['music', 'Music video']];
  const blankForm = () => ({ kind: 'show', title: '', year: '', genres: '', channel: '', transient: true, minutes: '', artist: '', url: '' });
  let f = $state(blankForm());
  let options = $state([]);
  $effect(() => { if (open) { f = blankForm(); tryApi(get('/api/lineup/options')).then((o) => (options = o ?? [])); } });

  let programme = $derived(f.kind === 'show' || f.kind === 'movie');
  // pitv_content's fetchable titles as suggestions; a title already on disk is flagged, not duplicated.
  let suggestions = $derived(options.filter((o) => !o.on_disk && o.type === f.kind));
  let existing = $derived(options.find((o) => o.on_disk && o.type === f.kind && o.title.toLowerCase() === f.title.trim().toLowerCase()));

  const add = guard(async () => {
    const year = num(f.year, { min: 1900, max: 2100, int: true });
    const r = programme
      ? await tryApi(post('/api/lineup', {
          kind: f.kind, title: f.title.trim(), year, genres: splitList(f.genres), transient: f.transient,
          channel_id: f.channel === '' ? null : Number(f.channel),
          episode_minutes: f.kind === 'show' ? num(f.minutes, { min: 1, max: 240, int: true }) : null }))
      : await tryApi(post('/api/wanted', { kind: f.kind, title: f.title.trim(), year, artist: f.artist.trim() || null, ref: f.url.trim() || null }));
    if (!r) return;
    noteChange('library');
    onadded?.(programme ? `${r.title} added to ${r.channel_name ?? 'its channel'}; pitv_content fetches it before it airs`
                        : `${r.title} added to the wanted list for pitv_content`);
  });
</script>

<Modal {open} title="Add to the catalogue" {onclose} width="560px">
  <div class="stack">
    <p class="scope" style="margin:0"><AppBadge app="pitv" /> For titles that are not on the NAS. pitv_content finds and fetches them into the cache; nothing is written to the NAS.</p>
    <div class="row">
      {#each KINDS as [id, label] (id)}<label class="check"><input type="radio" name="kind" value={id} bind:group={f.kind} /> {label}</label>{/each}
    </div>
    <div class="form-grid">
      <label class="field wide">Title<input bind:value={f.title} list="catalogue-suggestions" placeholder={f.kind === 'music' ? 'Song title' : 'As it was broadcast'} />
        {#if existing}<span class="help">Already in the catalogue{existing.channel_number ? ` on channel ${existing.channel_number}` : ''}; move it from the channel's line-up instead.</span>{/if}
      </label>
      <datalist id="catalogue-suggestions">{#each suggestions as o (o.title + (o.year ?? ''))}<option value={o.title}>{o.year ?? ''}</option>{/each}</datalist>
      <label class="field">Year<input type="number" class="narrow" min="1900" max="2100" bind:value={f.year} /><span class="help">Helps pitv_content find the right one.</span></label>
      {#if programme}
        <label class="field">Genres<input bind:value={f.genres} placeholder="Comedy, Drama" /><span class="help">Decide the channel when none is picked.</span></label>
        <label class="field">Channel
          <select bind:value={f.channel}><option value="">Choose by genres</option>{#each channels as c (c.id)}<option value={c.id}>{c.number} {c.name}</option>{/each}</select>
        </label>
        {#if f.kind === 'show'}<label class="field">Episode length (minutes)<input type="number" class="narrow" min="1" max="240" bind:value={f.minutes} placeholder="default" /></label>{/if}
        <label class="check wide"><input type="checkbox" bind:checked={f.transient} /> Remove after it airs<span class="help">Keep fetched files only in the cache and delete them once shown.</span></label>
      {:else}
        {#if f.kind === 'music'}<label class="field">Artist<input bind:value={f.artist} /></label>{/if}
        <label class="field wide">Link (optional)<input bind:value={f.url} placeholder="https://…" /><span class="help">A specific page or file for pitv_content to use instead of searching.</span></label>
      {/if}
    </div>
  </div>
  {#snippet footer()}
    <button onclick={onclose}>Cancel</button>
    <button class="primary" onclick={add} disabled={add.busy || !f.title.trim() || !!existing}>Add</button>
  {/snippet}
</Modal>
