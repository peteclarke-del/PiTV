<script>
  import AppBadge from '../../components/AppBadge.svelte';
  import { onMount, untrack } from 'svelte';
  import { get, post, put, tryApi } from '../../lib/api.js';
  import { PATTERN_TOKENS } from '../../lib/format.js';
  import { num, moveItem } from '../../lib/util.js';
  import { guard } from '../../lib/guard.svelte.js';
  import Drawer from '../../components/Drawer.svelte';
  import WeightRows from './WeightRows.svelte';
  import DaypartTable from './DaypartTable.svelte';
  import GenrePicker from '../../components/GenrePicker.svelte';
  import Tabs from '../../components/Tabs.svelte';
  import BandTable from './BandTable.svelte';
  import DecadePicker from '../../components/DecadePicker.svelte';
  import { shown } from '../../lib/prefs.svelte.js';

  let { channel = {}, onclose, onsaved } = $props();
  const c = untrack(() => ({ ...channel }));
  const DP_PARTS = [['weekday', 'Weekday'], ['saturday', 'Saturday'], ['sunday', 'Sunday']];
  // A profile is either a plain list (weekday only) or {weekday, saturday, sunday}; each part empty = use global.
  function splitProfile(p) {
    const copy = (rows) => (Array.isArray(rows) ? rows.map((d) => ({ ...d })) : []);
    const parts = Array.isArray(p) ? { weekday: p } : (p && typeof p === 'object' ? p : {});
    const out = {};
    for (const [k] of DP_PARTS) out[k] = { use: copy(parts[k]).length > 0, rows: copy(parts[k]) };
    return out;
  }
  function joinProfile(dp) {
    const out = {};
    for (const [k] of DP_PARTS) if (dp[k].use && dp[k].rows.length) out[k] = dp[k].rows;
    return Object.keys(out).length ? out : null;
  }
  const isNew = !c.id;
  let f = $state({
    number: c.number ?? '', name: c.name ?? '', short_name: c.short_name ?? '', colour: c.colour ?? '#e63946',
    enabled: !!(c.enabled ?? 1), ads_enabled: !!c.ads_enabled, ads_per_break: c.ads_per_break ?? 2, family_safe_ads: !!c.family_safe_ads,
    pattern: c.pattern_tokens ?? (c.pattern ? c.pattern.split(',').map((t) => t.trim()) : ['show']),
    eraUse: !!c.era_weights, era: c.era_weights ?? { '1980-1989': 0.85, '1990-1999': 0.15 },
    kindUse: !!c.kind_weights, kind: { tv: c.kind_weights?.tv ?? 0.7, movie: c.kind_weights?.movie ?? 0.3 },
    genreUse: !!c.genre_weights, genre: c.genre_weights ?? {},
    dp: splitProfile(c.daypart_profile),
    overnight_replay_from: c.overnight_replay_from ?? '08:00', idents_enabled: !!(c.idents_enabled ?? 1),
    description: c.description ?? '', content: c.content ?? 'general',
    allowed_genres: c.allowed_genres ?? [], excluded_genres: c.excluded_genres ?? [], nas_only: c.nas_only ?? 'inherit',
    short_episode_minutes: c.short_episode_minutes ?? '', short_episode_run_minutes: c.short_episode_run_minutes ?? '',
    fetch_kind: c.fetch_kind ?? '',
    decades: c.decades ?? [], kids_any_time: !!c.kids_any_time, bands: (c.bands ?? []).map((b) => ({ ...b, fill: { ...b.fill } })),
    band_item_repeat_hours: c.band_item_repeat_hours ?? '', band_feature_repeat_days: c.band_feature_repeat_days ?? '',
  });
  const CONTENT = [['general', 'General (shows and films)'], ['music', 'Music videos'], ['cartoons', 'Cartoons'],
                   ['documentaries', 'Documentaries'], ['films', 'Films'], ['sport', 'Sport'], ['kids', "Children's"]];
  // Everything is one form: switching sections keeps edits, and Save sends them all.
  const SECTIONS = [
    { id: 'channel', label: 'Channel', level: 'basic' },
    { id: 'programmes', label: 'Programmes', level: 'basic' },
    { id: 'bands', label: 'Bands', level: 'basic', title: 'Stretches of the day under one title, filled by the scheduler' },
    { id: 'breaks', label: 'Breaks', level: 'basic' },
    { id: 'mix', label: 'Mix', level: 'standard', title: 'TV and film balance, era and genre weights' },
    { id: 'dayparts', label: 'Dayparts', level: 'advanced', title: 'Time-of-day weights and the overnight replay' },
  ];
  let section = $state('channel');
  const PROGRAMME_KINDS = ['episode', 'movie'];   // a channel's own genres are about series and films
  let facets = $state(null);
  let fetchKinds = $state([]);
  let genreOptions = $derived(facets?.genres ?? {});
  let decadeOptions = $derived(Object.keys(facets?.decades ?? {}).map(Number));
  onMount(async () => {
    facets = (await tryApi(get('/api/library/facets'))) ?? null;
    fetchKinds = (await tryApi(get('/api/bands/fetch-kinds'))) ?? [];
  });

  async function loadDefaultDayparts(part) {
    const s = await tryApi(get('/api/settings'));
    const key = { weekday: 'dayparts', saturday: 'dayparts_saturday', sunday: 'dayparts_sunday' }[part];
    const rows = s?.[key]?.length ? s[key] : s?.dayparts;
    if (rows) f.dp[part].rows = rows.map((d) => ({ ...d }));
  }

  const save = guard(async () => {
    const body = {
      name: f.name, short_name: f.short_name, colour: f.colour, enabled: f.enabled, ads_enabled: f.ads_enabled, family_safe_ads: f.family_safe_ads,
      ads_per_break: num(f.ads_per_break, { min: 1, max: 10, int: true, fallback: 1 }), pattern: f.pattern.join(', '),
      era_weights: f.eraUse ? f.era : null,
      kind_weights: f.kindUse ? { tv: num(f.kind.tv, { min: 0, max: 1, fallback: 0 }), movie: num(f.kind.movie, { min: 0, max: 1, fallback: 0 }) } : null,
      genre_weights: f.genreUse ? f.genre : null, daypart_profile: joinProfile(f.dp),
      overnight_replay_from: f.overnight_replay_from, idents_enabled: f.idents_enabled, description: f.description, content: f.content,
      allowed_genres: f.allowed_genres, excluded_genres: f.excluded_genres, nas_only: f.nas_only,
      short_episode_minutes: f.short_episode_minutes === '' ? null : Number(f.short_episode_minutes),
      short_episode_run_minutes: f.short_episode_run_minutes === '' ? null : Number(f.short_episode_run_minutes),
      fetch_kind: f.fetch_kind || null,
      decades: f.decades, kids_any_time: f.kids_any_time, bands: f.bands,
      band_item_repeat_hours: num(f.band_item_repeat_hours, { min: 0, max: 8760, int: true }),
      band_feature_repeat_days: num(f.band_feature_repeat_days, { min: 0, max: 8760, int: true }),
    };
    const number = num(f.number, { min: 1, int: true });
    if (number !== null) body.number = number;
    const r = await tryApi(isNew ? post('/api/channels', body) : put(`/api/channels/${c.id}`, body), { success: 'Channel saved' });
    if (r) onsaved?.(r);
  });
</script>

<Drawer open={true} title={isNew ? 'New channel' : `Channel ${c.number}: ${c.name}`} {onclose} wide>
  <div class="stack">
    <p class="scope" style="margin:0"><AppBadge app="pitv" /> A PiTV channel: it changes what this channel carries and how its day is built, from the next schedule build.</p>
    <Tabs tabs={SECTIONS} bind:active={section} label="Channel settings" />

    {#if section === 'channel'}
      <div class="form-grid">
        <label class="field">Number<input type="number" class="narrow" min="1" bind:value={f.number} placeholder="auto" /><span class="help">Edit to reorder; must be unique.</span></label>
        <label class="field">Name<input bind:value={f.name} placeholder="PiTV One" /></label>
        <label class="field">Short name<input bind:value={f.short_name} placeholder="One" /><span class="help">Used on the badge and remote.</span></label>
        <label class="field">Colour<span class="row"><input type="color" bind:value={f.colour} /><input class="narrow mono" bind:value={f.colour} aria-label="Colour as hex" pattern={'#[0-9a-fA-F]{6}'} /></span></label>
        <label class="field wide">Description<input bind:value={f.description} placeholder="Mainstream: drama, sitcoms…" /></label>
        <label class="field wide">What it carries
          <select bind:value={f.content}>{#each CONTENT as [v, label] (v)}<option value={v}>{label}</option>{/each}</select>
          <span class="help">A label, for people reading the admin. What the channel actually shows comes from its genres, decades, pattern and bands.</span>
        </label>
        <label class="check"><input type="checkbox" bind:checked={f.enabled} /> Enabled</label>
      </div>
    {:else if section === 'programmes'}
      <div class="form-grid">
        <div class="field wide genres">
          <span>Allowed genres</span><GenrePicker value={f.allowed_genres} options={genreOptions} kinds={PROGRAMME_KINDS} onchange={(v) => (f.allowed_genres = v)} label="Allowed genres" />
          <span class="help">Which series and films the line-up generator places on this channel; empty means any.</span>
        </div>
        <div class="field wide genres">
          <span>Excluded genres</span><GenrePicker value={f.excluded_genres} options={genreOptions} kinds={PROGRAMME_KINDS} onchange={(v) => (f.excluded_genres = v)} empty="None" label="Excluded genres" />
        </div>
        <div class="field wide">
          <span>Decades</span><DecadePicker bind:value={f.decades} decades={decadeOptions} label="Channel decades" />
          <span class="help">Only programmes from these decades; empty means any. A series that ran into one of them counts, and a programme with no year is still allowed.</span>
        </div>
        {#if shown('standard')}
          <label class="field">Short episodes are under (minutes)<input type="number" class="narrow" min="0" max="60" bind:value={f.short_episode_minutes} placeholder="as Settings says" /><span class="help">Episodes shorter than this run together under the series title, so a five minute cartoon does not take a slot of its own.</span></label>
          <label class="field">Run them together for (minutes)<input type="number" class="narrow" min="5" max="120" bind:value={f.short_episode_run_minutes} placeholder="as Settings says" /></label>
        {/if}
        <label class="field wide">Fetch more material as
          <select bind:value={f.fetch_kind}>
            <option value="">nothing; this channel is not topped up</option>
            {#each fetchKinds as k (k)}<option value={k}>{k}</option>{/each}
          </select>
          <span class="help">When a band on this channel has too little of its own genres and decades, pitv_content is asked to fetch this kind of material. A band may ask for a different one.</span>
        </label>
        <label class="check wide"><input type="checkbox" bind:checked={f.kids_any_time} /> Children's programmes at any hour<span class="help">Ignores the children's cutoff in Settings, Certificates. Suits a channel that shows cartoons all evening.</span></label>
        {#if shown('standard')}
          <label class="field">Only schedule what is on disk
            <select bind:value={f.nas_only}><option value="inherit">as Settings says</option><option value="yes">yes</option><option value="no">no</option></select>
            <span class="help">No: line-up entries not on disk may be scheduled ahead and fetched by pitv_content.</span>
          </label>
        {/if}
      </div>
      <p class="small muted">The titles themselves are in the channel's line-up: Line-up, on the channel's row.</p>
    {:else if section === 'bands'}
      <p class="small muted">A band is a stretch of the day under one title, such as "Disco Lunch" or "Saturday Morning Cartoons". The scheduler fills it with items that match, and the guide shows the band as one programme. A channel with no pattern is built from its bands alone.</p>
      <BandTable bind:value={f.bands} {facets} {fetchKinds} channelKind={f.fetch_kind} />
      {#if shown('standard')}
        <div class="form-grid mt">
          <label class="field">Repeat an item after (hours)<input type="number" class="narrow" min="0" max="8760" bind:value={f.band_item_repeat_hours} placeholder="as Settings says" /><span class="help">How long before this channel's bands may play the same short item again.</span></label>
          <label class="field">Repeat a feature after (days)<input type="number" class="narrow" min="0" max="8760" bind:value={f.band_feature_repeat_days} placeholder="as Settings says" /><span class="help">The same, for concerts and films.</span></label>
        </div>
      {/if}
    {:else if section === 'breaks'}
      <div class="form-grid">
        <label class="check"><input type="checkbox" bind:checked={f.ads_enabled} /> Ad breaks on this channel</label>
        <label class="field">Ads per break<input type="number" class="narrow" min="1" max="10" bind:value={f.ads_per_break} disabled={!f.ads_enabled} /></label>
        <label class="check wide"><input type="checkbox" bind:checked={f.family_safe_ads} /> Family-safe adverts only<span class="help">No alcohol, tobacco, adult or gambling adverts (pitv_content's verdict, else the keywords in Settings, Adverts).</span></label>
        <label class="check"><input type="checkbox" bind:checked={f.idents_enabled} /> Idents between programmes</label>
      </div>
      {#if shown('standard')}
        <h3>Pattern</h3>
        <p class="small muted">The sequence repeated through the day. <code>show</code> is any programme, <code>tv</code> and <code>movie</code> force a kind, <code>ad</code> is one advert, <code>break</code> a full ad break, <code>ident</code> the channel ident.</p>
        <div class="row chips">
          {#each f.pattern as tok, i (i)}
            <span class="chip"><button onclick={() => moveItem(f.pattern, i, -1)} disabled={i === 0} aria-label="Earlier">‹</button><b>{tok}</b><button onclick={() => moveItem(f.pattern, i, 1)} disabled={i === f.pattern.length - 1} aria-label="Later">›</button><button onclick={() => f.pattern.splice(i, 1)} aria-label="Remove">✕</button></span>
          {/each}
        </div>
        <div class="row">{#each PATTERN_TOKENS as t (t)}<button class="small" onclick={() => f.pattern.push(t)}>+ {t}</button>{/each}</div>
      {/if}
    {:else if section === 'mix'}
      <p class="small muted">Each is optional: unticked, the channel follows Settings, Programming.</p>
      <div class="stack">
        <div>
          <label class="check"><input type="checkbox" bind:checked={f.kindUse} /> Own TV and film balance</label>
          {#if f.kindUse}
            <div class="sliders">
              <label class="field">TV <span class="mono">{Number(f.kind.tv).toFixed(2)}</span><input type="range" min="0" max="1" step="0.05" bind:value={f.kind.tv} /></label>
              <label class="field">Film <span class="mono">{Number(f.kind.movie).toFixed(2)}</span><input type="range" min="0" max="1" step="0.05" bind:value={f.kind.movie} /></label>
            </div>
          {/if}
        </div>
        <div>
          <label class="check"><input type="checkbox" bind:checked={f.eraUse} /> Own era weights</label>
          {#if f.eraUse}<div class="mt"><WeightRows value={f.era} onchange={(v) => (f.era = v)} keyLabel="Years" keyPlaceholder="1980-1989" addLabel="Add era" /></div>{/if}
        </div>
        <div>
          <label class="check"><input type="checkbox" bind:checked={f.genreUse} /> Genre weights</label>
          {#if f.genreUse}<div class="mt"><WeightRows value={f.genre} onchange={(v) => (f.genre = v)} keyLabel="Genre" keyPlaceholder="Comedy" addLabel="Add genre" /></div>{/if}
        </div>
      </div>
    {:else if section === 'dayparts'}
      <div class="stack">
        <label class="field">Overnight replay from<input type="time" class="narrow" bind:value={f.overnight_replay_from} /><span class="help">Overnight, the day's schedule is replayed from this time of day.</span></label>
        <p class="small muted">Each table is optional; unticked, the channel uses the one in Settings, Programming.</p>
        {#each DP_PARTS as [part, label] (part)}
          <div>
            <label class="check"><input type="checkbox" bind:checked={f.dp[part].use} onchange={() => { if (f.dp[part].use && !f.dp[part].rows.length) loadDefaultDayparts(part); }} /> Own {label.toLowerCase()} dayparts</label>
            {#if f.dp[part].use}
              <p class="tiny muted"><button class="small ghost" onclick={() => loadDefaultDayparts(part)}>Copy the {label.toLowerCase()} table from Settings</button></p>
              <DaypartTable bind:rows={f.dp[part].rows} />
            {/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
  {#snippet footer()}
    <button onclick={onclose}>Cancel</button>
    <button class="primary" onclick={save} disabled={save.busy || !f.name}>Save</button>
  {/snippet}
</Drawer>

<style>
  .chips { min-height: 2rem; }
  .sliders { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: .5rem; }
</style>
