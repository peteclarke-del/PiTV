<script>
  import { untrack } from 'svelte';
  import { get, post, put, tryApi } from '../../lib/api.js';
  import { PATTERN_TOKENS } from '../../lib/format.js';
  import Drawer from '../../components/Drawer.svelte';
  import WeightRows from './WeightRows.svelte';
  import DaypartTable from './DaypartTable.svelte';

  let { channel = {}, onclose, onsaved } = $props();
  const c = untrack(() => ({ ...channel }));
  const isNew = !c.id;
  let f = $state({
    number: c.number ?? '', name: c.name ?? '', short_name: c.short_name ?? '', colour: c.colour ?? '#e63946',
    enabled: c.enabled ?? 1, ads_enabled: !!c.ads_enabled, ads_per_break: c.ads_per_break ?? 2,
    pattern: c.pattern_tokens ?? (c.pattern ? c.pattern.split(',').map((t) => t.trim()) : ['show']),
    eraUse: !!c.era_weights, era: c.era_weights ?? { '1980-1989': 0.85, '1990-1999': 0.15 },
    kindUse: !!c.kind_weights, kind: { tv: c.kind_weights?.tv ?? 0.7, movie: c.kind_weights?.movie ?? 0.3 },
    genreUse: !!c.genre_weights, genre: c.genre_weights ?? {},
    dpUse: !!c.daypart_profile, dayparts: c.daypart_profile ? c.daypart_profile.map((d) => ({ ...d })) : [],
    overnight_replay_from: c.overnight_replay_from ?? '08:00', idents_enabled: c.idents_enabled ?? 1,
    description: c.description ?? '',
  });
  f.enabled = !!f.enabled; f.idents_enabled = !!f.idents_enabled;
  let saving = $state(false);

  async function loadDefaultDayparts() {
    const s = await tryApi(get('/api/settings'));
    if (s?.dayparts) f.dayparts = s.dayparts.map((d) => ({ ...d }));
  }
  function move(i, d) {
    const j = i + d;
    if (j < 0 || j >= f.pattern.length) return;
    const [x] = f.pattern.splice(i, 1);
    f.pattern.splice(j, 0, x);
  }
  async function save() {
    const body = {
      name: f.name, short_name: f.short_name, colour: f.colour, enabled: f.enabled, ads_enabled: f.ads_enabled,
      ads_per_break: Number(f.ads_per_break) || 1, pattern: f.pattern.join(', '),
      era_weights: f.eraUse ? f.era : null, kind_weights: f.kindUse ? { tv: Number(f.kind.tv), movie: Number(f.kind.movie) } : null,
      genre_weights: f.genreUse ? f.genre : null, daypart_profile: f.dpUse ? f.dayparts : null,
      overnight_replay_from: f.overnight_replay_from, idents_enabled: f.idents_enabled, description: f.description,
    };
    if (f.number !== '') body.number = Number(f.number);
    saving = true;
    const r = await tryApi(isNew ? post('/api/channels', body) : put(`/api/channels/${c.id}`, body), { success: 'Channel saved' });
    saving = false;
    if (r) onsaved?.(r);
  }
</script>

<Drawer open={true} title={isNew ? 'New channel' : `Channel ${c.number}: ${c.name}`} {onclose} wide>
  <div class="stack">
    <div class="form-grid">
      <label class="field">Number<input type="number" class="narrow" min="1" bind:value={f.number} placeholder="auto" /><span class="help">Edit to reorder; must be unique.</span></label>
      <label class="field">Name<input bind:value={f.name} placeholder="PiTV One" /></label>
      <label class="field">Short name<input bind:value={f.short_name} placeholder="One" /><span class="help">Used on the badge and remote.</span></label>
      <label class="field">Colour<span class="row"><input type="color" bind:value={f.colour} /><input class="narrow mono" bind:value={f.colour} /></span></label>
      <label class="field wide">Description<input bind:value={f.description} placeholder="Mainstream: drama, sitcoms…" /></label>
      <label class="check"><input type="checkbox" bind:checked={f.enabled} /> Enabled</label>
      <label class="check"><input type="checkbox" bind:checked={f.idents_enabled} /> Idents between programmes</label>
      <label class="field">Overnight replay from<input type="time" bind:value={f.overnight_replay_from} /><span class="help">Overnight the day's schedule is replayed from this time of day.</span></label>
    </div>

    <hr />
    <h3>Adverts</h3>
    <div class="form-grid">
      <label class="check"><input type="checkbox" bind:checked={f.ads_enabled} /> Ad breaks on this channel</label>
      <label class="field">Ads per break<input type="number" class="narrow" min="1" max="10" bind:value={f.ads_per_break} disabled={!f.ads_enabled} /></label>
    </div>

    <h3>Pattern</h3>
    <p class="small muted">The sequence of items repeated through the day. <code>show</code> = any programme, <code>tv</code>/<code>movie</code> force a kind, <code>ad</code> = one advert, <code>break</code> = a full ad break, <code>ident</code> = channel ident.</p>
    <div class="row chips">
      {#each f.pattern as tok, i (i)}
        <span class="chip"><button onclick={() => move(i, -1)} disabled={i === 0} aria-label="Earlier">‹</button><b>{tok}</b><button onclick={() => move(i, 1)} disabled={i === f.pattern.length - 1} aria-label="Later">›</button><button onclick={() => f.pattern.splice(i, 1)} aria-label="Remove">✕</button></span>
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
      <div>
        <label class="check"><input type="checkbox" bind:checked={f.dpUse} onchange={() => { if (f.dpUse && !f.dayparts.length) loadDefaultDayparts(); }} /> Custom daypart profile</label>
        {#if f.dpUse}
          <p class="tiny muted">Leave empty to use the global dayparts from Weighting. <button class="small ghost" onclick={loadDefaultDayparts}>Copy global</button></p>
          <DaypartTable bind:rows={f.dayparts} />
        {/if}
      </div>
    </div>
  </div>
  {#snippet footer()}
    <button onclick={onclose}>Cancel</button>
    <button class="primary" onclick={save} disabled={saving || !f.name}>Save</button>
  {/snippet}
</Drawer>

<style>
  .chips { min-height: 2rem; }
  .sliders { display: grid; grid-template-columns: 1fr 1fr; gap: 1rem; margin-top: .5rem; }
</style>
