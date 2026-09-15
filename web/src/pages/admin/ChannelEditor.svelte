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
  });
  let genreOptions = $state({});
  onMount(async () => { genreOptions = (await tryApi(get('/api/library/genres'))) ?? {}; });

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
    <div class="form-grid">
      <label class="field">Number<input type="number" class="narrow" min="1" bind:value={f.number} placeholder="auto" /><span class="help">Edit to reorder; must be unique.</span></label>
      <label class="field">Name<input bind:value={f.name} placeholder="PiTV One" /></label>
      <label class="field">Short name<input bind:value={f.short_name} placeholder="One" /><span class="help">Used on the badge and remote.</span></label>
      <label class="field">Colour<span class="row"><input type="color" bind:value={f.colour} /><input class="narrow mono" bind:value={f.colour} aria-label="Colour as hex" pattern={'#[0-9a-fA-F]{6}'} /></span></label>
      <label class="field wide">Description<input bind:value={f.description} placeholder="Mainstream: drama, sitcoms…" /></label>
      <div class="field wide genres">
        <span>Line-up genres</span>
        <div class="row">
          <span class="small">Allowed</span><GenrePicker value={f.allowed_genres} options={genreOptions} onchange={(v) => (f.allowed_genres = v)} label="Allowed genres" />
          <span class="small">Excluded</span><GenrePicker value={f.excluded_genres} options={genreOptions} onchange={(v) => (f.excluded_genres = v)} empty="None" label="Excluded genres" />
          <span class="small">NAS only</span>
          <select bind:value={f.nas_only} style="min-height:28px;padding:.15rem .4rem"><option value="inherit">inherit</option><option value="yes">yes</option><option value="no">no</option></select>
        </div>
        <span class="help">Allowed genres decide which series and films the generator places on this channel; empty means any. NAS only, yes: only material on the NAS or in the cache. No: line-up entries not on disk may be scheduled ahead and fetched by pitv_content. Inherit follows the global setting.</span>
      </div>
      <label class="field wide">Content
        <select bind:value={f.content}><option value="general">General (shows and films)</option><option value="music">Music videos</option><option value="cartoons">Cartoons</option></select>
        <span class="help">Music: the day is built from genre/decade blocks and two concerts, see Weighting, Music channel. Cartoons: animated series are routed here automatically and may run all evening.</span>
      </label>
      <label class="check"><input type="checkbox" bind:checked={f.enabled} /> Enabled</label>
      <label class="check"><input type="checkbox" bind:checked={f.idents_enabled} /> Idents between programmes</label>
      <label class="field">Overnight replay from<input type="time" bind:value={f.overnight_replay_from} /><span class="help">Overnight the day's schedule is replayed from this time of day.</span></label>
    </div>

    <hr />
    <h3>Adverts</h3>
    <div class="form-grid">
      <label class="check"><input type="checkbox" bind:checked={f.ads_enabled} /> Ad breaks on this channel</label>
      <label class="field">Ads per break<input type="number" class="narrow" min="1" max="10" bind:value={f.ads_per_break} disabled={!f.ads_enabled} /></label>
      <label class="check wide"><input type="checkbox" bind:checked={f.family_safe_ads} /> Family-safe adverts only<span class="help">No alcohol, tobacco, adult or gambling adverts on this channel (flagged by the keywords under Weighting, Family-safe adverts).</span></label>
    </div>

    <h3>Pattern</h3>
    <p class="small muted">The sequence of items repeated through the day. <code>show</code> = any programme, <code>tv</code>/<code>movie</code> force a kind, <code>ad</code> = one advert, <code>break</code> = a full ad break, <code>ident</code> = channel ident.</p>
    <div class="row chips">
      {#each f.pattern as tok, i (i)}
        <span class="chip"><button onclick={() => moveItem(f.pattern, i, -1)} disabled={i === 0} aria-label="Earlier">‹</button><b>{tok}</b><button onclick={() => moveItem(f.pattern, i, 1)} disabled={i === f.pattern.length - 1} aria-label="Later">›</button><button onclick={() => f.pattern.splice(i, 1)} aria-label="Remove">✕</button></span>
      {/each}
    </div>
    <div class="row">{#each PATTERN_TOKENS as t (t)}<button class="small" onclick={() => f.pattern.push(t)}>+ {t}</button>{/each}</div>

    <hr />
    <h3>Weights</h3>
    <div class="stack">
      <div>
        <label class="check"><input type="checkbox" bind:checked={f.kindUse} /> Custom TV / movie balance</label>
        {#if f.kindUse}
          <div class="sliders">
            <label class="field">TV <span class="mono">{Number(f.kind.tv).toFixed(2)}</span><input type="range" min="0" max="1" step="0.05" bind:value={f.kind.tv} /></label>
            <label class="field">Movie <span class="mono">{Number(f.kind.movie).toFixed(2)}</span><input type="range" min="0" max="1" step="0.05" bind:value={f.kind.movie} /></label>
          </div>
        {/if}
      </div>
      <div>
        <label class="check"><input type="checkbox" bind:checked={f.eraUse} /> Custom era weights</label>
        {#if f.eraUse}<div class="mt"><WeightRows value={f.era} onchange={(v) => (f.era = v)} keyLabel="Years" keyPlaceholder="1980-1989" addLabel="Add era" /></div>{/if}
      </div>
      <div>
        <label class="check"><input type="checkbox" bind:checked={f.genreUse} /> Genre weights</label>
        {#if f.genreUse}<div class="mt"><WeightRows value={f.genre} onchange={(v) => (f.genre = v)} keyLabel="Genre" keyPlaceholder="Comedy" addLabel="Add genre" /></div>{/if}
      </div>
      <div class="stack">
        <div class="small muted">Daypart profile: each part is optional and falls back to the global tables in Weighting.</div>
        {#each DP_PARTS as [part, label] (part)}
          <div>
            <label class="check"><input type="checkbox" bind:checked={f.dp[part].use} onchange={() => { if (f.dp[part].use && !f.dp[part].rows.length) loadDefaultDayparts(part); }} /> Custom {label.toLowerCase()} dayparts</label>
            {#if f.dp[part].use}
              <p class="tiny muted">Empty = use global. <button class="small ghost" onclick={() => loadDefaultDayparts(part)}>Copy global {label.toLowerCase()}</button></p>
              <DaypartTable bind:rows={f.dp[part].rows} />
            {/if}
          </div>
        {/each}
      </div>
    </div>
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
