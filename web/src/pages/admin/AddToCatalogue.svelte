<script>
  // Custom programming: a title that is not on the NAS joins the catalogue here, in two steps.
  // First pitv_content looks it up online (contract section 8) so the admin can confirm the
  // right one; then it is placed. A series or film becomes a line-up entry carrying that
  // identity, on a chosen channel or the one that takes what it is, and pitv_content fetches it before
  // it airs. An advert or music video joins the wanted list, pinned to the chosen video.
  import { get, post, tryApi } from '../../lib/api.js';
  import { isOffline, toolGet } from '../../lib/toolapi.js';
  import { num } from '../../lib/util.js';
  import { safeUrl, PROGRAMME_TYPES, programmeTypeLabel } from '../../lib/format.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { noteChange } from '../../lib/stores.svelte.js';
  import Modal from '../../components/Modal.svelte';
  import AppBadge from '../../components/AppBadge.svelte';
  import LookupResults from './LookupResults.svelte';
  import GenrePicker from '../../components/GenrePicker.svelte';

  let { open = false, channels = [], onclose, onadded } = $props();
  // pitv_content joins its sources' answers and cuts the list at the limit, so a low one drops a whole
  // source: at 8 a common title came back as one catalogue entry and seven from TVmaze, with nothing
  // from TMDb, and the ones already held are greyed out of those.
  const LOOKUP_LIMIT = 25;
  const KINDS = [['show', 'Series'], ['movie', 'Film'], ['channel', 'YouTube channel'],
                 ['advert', 'Advert'], ['music', 'Music video']];
  const blankForm = () => ({ kind: 'show', title: '', year: '', genres: [], programme_type: '', channel: '', transient: false, minutes: '', artist: '', url: '' });
  let f = $state(blankForm());
  let step = $state('search');        // search | place
  let found = $state(null);           // candidates, once looked up
  let lookupNote = $state('');        // why there are none to show, when the lookup could not run
  let chosen = $state(null);          // the candidate confirmed, or null for "without a match"
  let options = $state([]);
  let facets = $state(null);
  let known = $state([]);             // what the catalogue already holds of this kind, to grey out in the results
  $effect(() => {
    if (!open) return;
    const kind = f.kind;
    tryApi(get('/api/lineup/known', { kind })).then((rows) => { if (kind === f.kind) known = rows ?? []; });
  });
  $effect(() => {
    if (!open) return;
    f = blankForm(); step = 'search'; found = null; lookupNote = ''; chosen = null;
    Promise.all([tryApi(get('/api/lineup/options')), tryApi(get('/api/library/facets'))]).then(([o, x]) => {
      options = o ?? []; facets = x ?? null;
    });
  });

  // A channel or playlist somebody has chosen: its videos become the episodes of a series, taken
  // earliest first, so it needs an address and a channel to belong to and nothing looked up.
  let curated = $derived(f.kind === 'channel');
  let programme = $derived(f.kind === 'show' || f.kind === 'movie' || curated);
  // Nothing online knows about somebody's YouTube channel, so there is nothing to look up and no
  // match to confirm: the address is the identity. It goes straight to the details rather than
  // sending the owner through a search that cannot succeed and out by "Add without a match".
  let stage = $derived(curated ? 'place' : step);
  // What the title would be taken for and where it would go, asked of PiTV so the rule lives in one place.
  let placement = $state(null);
  $effect(() => {
    if (!open || stage !== 'place' || !programme) { placement = null; return; }
    const ask = { kind: f.kind, genres: [...f.genres], programme_type: f.programme_type || null, year: num(f.year, { int: true }) };
    if (curated) { placement = null; return; }
    tryApi(post('/api/lineup/placement', ask)).then((p) => { placement = p ?? null; });
  });
  // pitv_content's fetchable titles as suggestions; a title already on disk is flagged, not duplicated.
  let suggestions = $derived(options.filter((o) => !o.on_disk && o.type === f.kind));
  let existing = $derived(options.find((o) => o.on_disk && o.type === f.kind && o.title.toLowerCase() === f.title.trim().toLowerCase()));

  const search = guard(async () => {
    found = null; lookupNote = '';
    try {
      const r = await toolGet('lookup', { kind: f.kind, title: f.title.trim(), year: num(f.year, { int: true }) ?? undefined,
                                          artist: f.artist.trim() || undefined, limit: LOOKUP_LIMIT });
      found = Array.isArray(r?.candidates) ? r.candidates : [];
      const failed = Object.entries(r?.errors ?? {});
      if (failed.length) lookupNote = `Not every source answered: ${failed.map(([s, m]) => `${s}: ${m}`).join('; ')}`;
    } catch (e) {
      lookupNote = isOffline(e) ? 'pitv_content is not reachable, so nothing can be looked up.'
        : e.status === 404 ? 'This version of pitv_content cannot look titles up.' : (e.detail || e.message);
    }
  });
  function pick(c) {
    chosen = c;
    f.title = c.title ?? f.title;
    if (c.year) f.year = c.year;
    if (c.genres?.length) f.genres = [...c.genres];
    if (c.runtime_minutes) f.minutes = c.runtime_minutes;
    if (c.artist) f.artist = c.artist;
    if (!programme) f.url = safeUrl(c.match?.url) ?? '';
    step = 'place';
  }
  function withoutMatch() { chosen = null; step = 'place'; }

  const add = guard(async () => {
    const year = num(f.year, { min: 1900, max: 2100, int: true });
    const r = programme
      ? await tryApi(post('/api/lineup', curated ? {
          title: f.title.trim(), youtube_url: f.url.trim(), genres: f.genres,
          channel_id: f.channel === '' ? null : Number(f.channel),
          episode_minutes: num(f.minutes, { min: 1, max: 240, int: true }) } : {
          kind: f.kind, title: f.title.trim(), year, genres: f.genres, programme_type: f.programme_type || null, transient: f.transient,
          channel_id: f.channel === '' ? null : Number(f.channel), match: chosen?.match ?? null,
          network: chosen?.network ?? null,
          episode_minutes: f.kind === 'show' ? num(f.minutes, { min: 1, max: 240, int: true }) : null,
          episode_count: f.kind === 'show' ? num(chosen?.episodes, { min: 1, int: true }) : null,
          certificate: chosen?.certificate ?? null }))
      : await tryApi(post('/api/wanted', { kind: f.kind, title: f.title.trim(), year, artist: f.artist.trim() || null, ref: f.url.trim() || null }));
    if (!r) return;
    noteChange('library');
    onadded?.(curated ? `${r.title} added to ${r.channel_name ?? 'its channel'}; its videos are fetched earliest first`
              : programme ? `${r.title} added to ${r.channel_name ?? 'its channel'}; pitv_content fetches it before it airs`
                          : `${r.title} added to the wanted list for pitv_content`, r);
  });
</script>

<Modal {open} title="Add to the catalogue" {onclose} width="640px">
  <div class="stack">
    <p class="scope" style="margin:0"><AppBadge app="pitv" /> For titles that are not on the NAS. pitv_content looks the title up online so the right one is added, then fetches it into the cache; nothing is written to the NAS.</p>
    {#if stage === 'search' || curated}
      <div class="row">
        {#each KINDS as [id, label] (id)}<label class="check"><input type="radio" name="kind" value={id} bind:group={f.kind} onchange={() => (found = null)} /> {label}</label>{/each}
      </div>
    {/if}
    {#if stage === 'search'}
      <div class="form-grid">
        <label class="field wide">Title<input bind:value={f.title} list="catalogue-suggestions" placeholder={f.kind === 'music' ? 'Song title' : 'As it was broadcast'}
          onkeydown={(e) => { if (e.key === 'Enter' && f.title.trim()) search(); }} />
          {#if existing}<span class="help">Already in the catalogue{existing.channel_number ? ` on channel ${existing.channel_number}` : ''}; move it from the channel's line-up instead.</span>{/if}
        </label>
        <datalist id="catalogue-suggestions">{#each suggestions as o (o.title + (o.year ?? ''))}<option value={o.title}>{o.year ?? ''}</option>{/each}</datalist>
        <label class="field">Year (optional)<input type="number" class="narrow" min="1900" max="2100" bind:value={f.year} /></label>
        {#if f.kind === 'music'}<label class="field">Artist<input bind:value={f.artist} /></label>{/if}
      </div>
      {#if lookupNote}<div class="note small">{lookupNote}</div>{/if}
      {#if found}<LookupResults candidates={found} {known} onpick={pick} />{/if}
    {:else}
      {#if chosen}
        <div class="note small">Matched: <b>{chosen.title}</b>{chosen.year ? ` (${chosen.year})` : ''} from {chosen.match?.source}. pitv_content fetches this one.
          <button class="small ghost" onclick={() => (step = 'search')}>Change</button></div>
      {:else}
        <div class="warn-box small" hidden={curated}>No online match: pitv_content will search by title{f.year ? ' and year' : ''} alone and may find a different {f.kind === 'movie' ? 'film' : f.kind === 'show' ? 'series' : 'video'}.
          <button class="small ghost" onclick={() => (step = 'search')}>Search again</button></div>
      {/if}
      <div class="form-grid">
        {#if curated}
          <label class="field wide">Channel or playlist address<input bind:value={f.url} placeholder="https://www.youtube.com/@…" />
            <span class="help">Paste the address from the browser. Its videos become the episodes of this entry, taken earliest first. A playlist keeps the order its maker chose, so point at one where it exists.</span></label>
          <label class="field wide">Title<input bind:value={f.title} placeholder="What the guide calls it" />
            <span class="help">The name this becomes a series under, not the address. Call it what you want to read in the guide.</span></label>
        {:else}
          <label class="field wide">Title<input bind:value={f.title} /></label>
          <label class="field">Year<input type="number" class="narrow" min="1900" max="2100" bind:value={f.year} /></label>
        {/if}
        {#if curated}
          <label class="field">Channel
            <select bind:value={f.channel}><option value="">Where it belongs</option>{#each channels as c (c.id)}<option value={c.id}>{c.number} {c.name}</option>{/each}</select>
            <span class="help">Its material carries the YouTube genre, so a band asking for that claims it.</span>
          </label>
          <div class="field wide"><span>What it is about</span>
            <GenrePicker value={f.genres} options={facets?.genres ?? {}} kinds={['episode']}
                         onchange={(v) => (f.genres = v)} label="Entry genres" empty="Choose or type a subject" allowNew />
            <span class="help">Type a subject nothing carries yet and it is created, which is how motorcycling or
              retro computing come to exist. Every entry also carries YouTube, so a band naming both takes this
              subject from YouTube alone.</span></div>
          <label class="field">Episode length (minutes)<input type="number" class="narrow" min="1" max="240" bind:value={f.minutes} placeholder="default" /></label>
          <div class="warn-box small wide">Videos are numbered by their place in the listing. If the creator deletes one, everything after it shifts by one and an episode already scheduled becomes a different programme. Nothing detects that.</div>
        {:else if programme}
          <div class="field"><span>Genres</span><GenrePicker value={f.genres} options={facets?.genres ?? {}} kinds={f.kind === 'show' ? ['episode'] : ['movie']} onchange={(v) => (f.genres = v)} label="Entry genres" empty="Choose genres" /><span class="help">The same programme genres used by Channel settings.</span></div>
          <label class="field">What it is
            <select bind:value={f.programme_type}><option value="">{placement ? `${programmeTypeLabel(placement.programme_type)} (read from its genres)` : 'Read from its genres'}</option>{#each PROGRAMME_TYPES as [v, l] (v)}<option value={v}>{l}</option>{/each}</select>
            <span class="help">This decides which channel theme it belongs to. Genres only describe it.</span>
          </label>
          <label class="field">Channel
            <select bind:value={f.channel}><option value="">{placement?.channel_name ? `Where it belongs: ${placement.channel_number} ${placement.channel_name}` : 'Where it belongs'}</option>{#each channels as c (c.id)}<option value={c.id}>{c.number} {c.name}</option>{/each}</select>
            {#if placement && !placement.channel_id}<span class="help">No channel takes a {programmeTypeLabel(placement.programme_type).toLowerCase()} with these genres; choose one.</span>{/if}
          </label>
          {#if f.kind === 'show'}<label class="field">Episode length (minutes)<input type="number" class="narrow" min="1" max="240" bind:value={f.minutes} placeholder="default" /></label>{/if}
          <label class="check wide"><input type="checkbox" bind:checked={f.transient} /> Remove after it airs<span class="help">Normally left off: what is fetched is kept, so a later airing costs nothing, and the oldest goes first if the drive ever needs room.</span></label>
        {:else}
          {#if f.kind === 'music'}<label class="field">Artist<input bind:value={f.artist} /></label>{/if}
          <label class="field wide">Video{#if chosen} (from the match){/if}<input bind:value={f.url} placeholder="https://…" /><span class="help">A specific video for pitv_content to fetch instead of searching.</span></label>
        {/if}
      </div>
    {/if}
  </div>
  {#snippet footer()}
    <button onclick={onclose}>Cancel</button>
    {#if stage === 'search'}
      <button onclick={withoutMatch} disabled={!f.title.trim() || !!existing} title="Skip the online check">Add without a match</button>
      <button class="primary" onclick={search} disabled={search.busy || !f.title.trim() || !!existing}>{search.busy ? 'Searching…' : 'Search online'}</button>
    {:else}
      <button class="primary" onclick={add}
              disabled={add.busy || !f.title.trim() || !!existing || (curated && !f.url.trim())}>Add</button>
    {/if}
  {/snippet}
</Modal>
