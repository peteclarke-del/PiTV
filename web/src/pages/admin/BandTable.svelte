<script>
  // A channel's bands: a stretch of the day under one title, filled with several items.
  // The scheduler picks what goes in; this says when, for how long, and from what.
  import { moveItem } from '../../lib/util.js';
  import { post, tryApi } from '../../lib/api.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { toast } from '../../lib/stores.svelte.js';
  import { WEEKDAYS } from '../../lib/format.js';
  import GenrePicker from '../../components/GenrePicker.svelte';
  import DecadePicker from '../../components/DecadePicker.svelte';

  let { value = $bindable([]), facets = null, fetchKinds = [], channelKind = '' } = $props();
  const KINDS = [['music', 'Music videos'], ['episode', 'Episodes'], ['movie', 'Films']];

  function add() {
    const last = value.at(-1);
    value.push({ name: '', start: last ? last.start : '08:00', minutes: null, days: [], enabled: true,
                 fill: { kinds: ['music'], genres: [], decades: [], feature: false, fetch: '' } });
  }
  let usesMusic = $derived(value.some((b) => (b.fill?.kinds ?? []).includes('music')));
  // A band's choices are what the library holds for the kinds it draws on, so nothing offered is empty.
  const genreOptions = $derived(facets?.genres ?? {});
  const decadesFor = (kinds) => Object.entries(facets?.decades ?? {})
    .filter(([, counts]) => kinds.some((k) => (counts[k] ?? 0) > 0)).map(([d]) => Number(d));
  // Asks for the band with least, wherever it is: the nightly pass does the same thing.
  const topUp = guard(async () => {
    const r = await tryApi(post('/api/bands/material', {}));
    if (r) toast[r.status === 'error' ? 'error' : 'success'](r.summary ?? 'Asked pitv_content');
  });
  const toggle = (list, v) => (list.includes(v) ? list.filter((x) => x !== v) : [...list, v].sort((a, b) => a - b));
  function toggleKind(b, k) {
    const kinds = b.fill.kinds.includes(k) ? b.fill.kinds.filter((x) => x !== k) : [...b.fill.kinds, k];
    b.fill.kinds = kinds.length ? kinds : [k];
  }
</script>

<div class="stack">
  {#each value as b, i (i)}
    <div class="band" class:off={!b.enabled}>
      <div class="line">
        <label class="tiny">Start<input type="time" bind:value={b.start} /></label>
        <label class="tiny">Minutes<input type="number" class="xnarrow" min="1" max="720" placeholder="to next" value={b.minutes ?? ''} onchange={(e) => (b.minutes = e.currentTarget.value === '' ? null : Number(e.currentTarget.value))} /></label>
        <label class="tiny grow">Title<input bind:value={b.name} placeholder="Disco Lunch" /></label>
        <span class="spacer"></span>
        <button class="small ghost" disabled={i === 0} onclick={() => moveItem(value, i, -1)} aria-label="Move up">↑</button>
        <button class="small ghost" disabled={i === value.length - 1} onclick={() => moveItem(value, i, 1)} aria-label="Move down">↓</button>
        <button class="small ghost" onclick={() => value.splice(i, 1)} aria-label="Remove band">✕</button>
      </div>
      <div class="line">
        <span class="tiny muted lbl">Days</span>
        {#each WEEKDAYS as d, n (d)}<label class="pick" class:on={b.days.includes(n)}><input type="checkbox" checked={b.days.includes(n)} onchange={() => (b.days = toggle(b.days, n))} />{d}</label>{/each}
        {#if !b.days.length}<span class="tiny muted">every day</span>{/if}
      </div>
      <div class="line">
        <span class="tiny muted lbl">From</span>
        {#each KINDS as [k, label] (k)}<label class="pick" class:on={b.fill.kinds.includes(k)}><input type="checkbox" checked={b.fill.kinds.includes(k)} onchange={() => toggleKind(b, k)} />{label}</label>{/each}
        <label class="pick feature" class:on={b.fill.feature}><input type="checkbox" checked={!!b.fill.feature} onchange={(e) => (b.fill.feature = e.currentTarget.checked)} />Open with a feature</label>
        <label class="pick" class:on={b.enabled}><input type="checkbox" bind:checked={b.enabled} />On</label>
      </div>
      <div class="line"><span class="tiny muted lbl">Genres</span><GenrePicker value={b.fill.genres} options={genreOptions} kinds={b.fill.kinds} onchange={(v) => (b.fill.genres = v)} label="Band genres" /><span class="tiny muted">of what the band draws on</span></div>
      <div class="line"><span class="tiny muted lbl">Decades</span><DecadePicker bind:value={b.fill.decades} decades={decadesFor(b.fill.kinds)} label="Band decades" /></div>
      {#if fetchKinds.length}
        <div class="line"><span class="tiny muted lbl">Top up</span>
          <select class="small" value={b.fill.fetch ?? ''} onchange={(e) => (b.fill.fetch = e.currentTarget.value)}>
            <option value="">{channelKind ? `as the channel: ${channelKind}` : 'nothing; the channel asks for nothing'}</option>
            {#each fetchKinds as k (k)}<option value={k}>{k}</option>{/each}
          </select>
          <span class="tiny muted">what pitv_content fetches when this band runs short</span>
        </div>
      {/if}
    </div>
  {:else}
    <p class="muted small">No bands: the channel's day comes from its pattern alone.</p>
  {/each}
  <div class="row"><button class="small" onclick={add}>Add band</button>
    {#if fetchKinds.length && (channelKind || value.some((b) => b.fill?.fetch))}
      <button class="small" onclick={topUp} disabled={topUp.busy}>{topUp.busy ? 'Asking…' : 'Find material now'}</button>
    {/if}
    <!-- Only a band that draws on music has any use for what the music library holds. -->
    {#if facets && usesMusic}<span class="tiny muted">Music in the library: {facets.concerts} concerts, {Object.keys(facets.genres).length} genres, decades {Object.keys(facets.decades).join(', ') || 'none'}</span>{/if}
  </div>
</div>

<style>
  .band { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: .5rem .6rem; display: flex; flex-direction: column; gap: .35rem; }
  .band.off { opacity: .6; }
  .line { display: flex; flex-wrap: wrap; align-items: center; gap: .3rem .5rem; }
  .line label.tiny { display: flex; flex-direction: column; font-size: .7rem; color: var(--fg-muted); gap: .1rem; }
  .grow { flex: 1; min-width: 160px; }
  .lbl { min-width: 3.5rem; }
  .pick { font-size: .7rem; padding: .1rem .45rem; border: 1px solid var(--border); border-radius: 999px; cursor: pointer; user-select: none; }
  .pick input { position: absolute; opacity: 0; width: 0; height: 0; margin: 0; }
  .pick:has(input:focus-visible) { outline: 2px solid var(--info); outline-offset: 1px; }
  .pick.on { background: var(--accent); color: #fff; border-color: var(--accent); }
  .pick.feature.on { background: var(--info); border-color: var(--info); }
</style>
