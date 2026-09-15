<script>
  import { onMount } from 'svelte';
  import { get, put, post, tryApi, confirmApi } from '../../lib/api.js';
  import { buildSchedule } from '../../lib/actions.js';
  import { toast } from '../../lib/stores.svelte.js';
  import { CERTIFICATES } from '../../lib/format.js';
  import WeightRows from './WeightRows.svelte';
  import DaypartTable from './DaypartTable.svelte';
  import ChipList from '../../components/ChipList.svelte';
  const DECADES = [1930, 1940, 1950, 1960, 1970, 1980, 1990, 2000, 2010, 2020];

  let s = $state(null);
  let saving = $state(false);
  let savedOnce = $state(false);

  let facets = $state(null);
  function addBlock() {
    const last = s.music_blocks.at(-1);
    s.music_blocks.push({ start: last ? last.start : '08:00', name: '', genres: [], decades: [1980], concert: false });
  }
  function moveBlock(i, d) { const j = i + d; if (j < 0 || j >= s.music_blocks.length) return; const [x] = s.music_blocks.splice(i, 1); s.music_blocks.splice(j, 0, x); }
  function toggleMusicDecade(d) { s.music_decades = (s.music_decades ?? []).includes(d) ? s.music_decades.filter((x) => x !== d) : [...(s.music_decades ?? []), d].sort(); }
  function toggleDecade(b, d) { b.decades = (b.decades ?? []).includes(d) ? b.decades.filter((x) => x !== d) : [...(b.decades ?? []), d].sort(); }
  async function load() {
    s = await tryApi(get('/api/settings'));
    if (s) for (const k of ['dayparts_saturday', 'dayparts_sunday', 'music_blocks', 'music_genres', 'cartoon_genres', 'music_decades', 'adult_advert_keywords', 'readiness_hours']) s[k] ??= [];
    tryApi(get('/api/music/facets')).then((f) => (facets = f ?? null));
  }
  onMount(load);

  async function save() {
    saving = true;
    const body = { ...s };
    for (const k of ['horizon_days', 'rebuild_when_days_left', 'show_daily_limit', 'movie_repeat_days',
                     'duration_tolerance_minutes', 'start_rounding_minutes', 'end_of_day_overrun_minutes', 'advert_year_window',
                     'advert_repeat_penalty_hours', 'series_rest_weeks', 'badge_seconds', 'cache_max_gb', 'scan_hour', 'history_keep_days']) body[k] = parseInt(body[k], 10) || 0;
    body.readiness_hours = [...new Set((s.readiness_hours ?? []).map((h) => parseInt(h, 10)).filter((h) => h >= 0 && h <= 23))].sort((a, b) => a - b);
    for (const k of ['show_repeat_penalty', 'same_slot_bonus', 'genre_repeat_penalty', 'unknown_year_weight', 'osd_safe_margin', 'osd_scale']) body[k] = Number(body[k]) || 0;
    body.kind_weights = { tv: Number(s.kind_weights.tv), movie: Number(s.kind_weights.movie) };
    const cleanDp = (rows) => (rows ?? []).map((d) => {
      const o = { name: d.name, start: d.start, tv: Number(d.tv) || 0, movie: Number(d.movie) || 0, kids: Number(d.kids) || 0, sport: Number(d.sport) || 0 };
      if (d.max_minutes) o.max_minutes = Number(d.max_minutes);
      return o;
    });
    body.dayparts = cleanDp(s.dayparts);
    body.dayparts_saturday = cleanDp(s.dayparts_saturday);
    body.dayparts_sunday = cleanDp(s.dayparts_sunday);
    body.era_pool_normalise = Math.max(0, Math.min(1, Number(s.era_pool_normalise) || 0));
    body.music_blocks = (s.music_blocks ?? []).map((b) => {
      const o = { start: b.start, name: b.name, genres: (b.genres ?? []).map((g) => String(g).toLowerCase()), decades: (b.decades ?? []).map(Number).sort() };
      if (b.concert) o.concert = true;
      return o;
    });
    for (const k of ['music_concert_repeat_days', 'music_video_repeat_hours']) body[k] = parseInt(body[k], 10) || 0;
    body.music_decades = (s.music_decades ?? []).map(Number).sort();
    body.adult_advert_keywords = (s.adult_advert_keywords ?? []).map((k) => String(k).toLowerCase());
    const r = await tryApi(put('/api/settings', body), { success: 'Settings saved' });
    saving = false;
    if (r) { s = r; savedOnce = true; }
  }
  async function reset() {
    const r = await confirmApi('Reset every weighting setting to its default?', { title: 'Reset to defaults', okLabel: 'Reset', danger: true },
      () => post('/api/settings/reset', {}), { success: 'Settings reset' });
    if (r) { s = r; savedOnce = true; }
  }
  const build = () => buildSchedule().then((r) => { if (r) toast.info('Changes apply to newly built days; use "Rebuild week" on the dashboard to redo existing days.'); });
</script>

{#if s}
  <div class="stack">
    <div class="row">
      <button class="primary" onclick={save} disabled={saving}>Save settings</button>
      <button onclick={reset}>Reset to defaults</button>
      <span class="spacer"></span>
      <button onclick={build}>Build schedule</button>
    </div>
    {#if savedOnce}<div class="note">Saved. The schedule only picks up these changes when it is (re)built: use Build schedule for new days or Rebuild week on the dashboard to regenerate everything.</div>{/if}

    <div class="grid">
      <div class="card">
        <div class="card-title"><h3>Broadcast day</h3></div>
        <div class="stack">
          <label class="field">Timezone<input bind:value={s.timezone} /><span class="help">IANA zone name the schedule is built in, e.g. Europe/London.</span></label>
          <label class="field">Day start<input type="time" bind:value={s.day_start} /><span class="help">When a broadcast day begins and the overnight replay ends.</span></label>
          <label class="field">Day end<input type="time" bind:value={s.day_end} /><span class="help">When fresh programming stops and the overnight replay begins (00:00 = midnight).</span></label>
          <label class="field">Horizon days<input type="number" min="1" max="28" bind:value={s.horizon_days} /><span class="help">How many days ahead the schedule is kept built.</span></label>
          <label class="field">Rebuild threshold<input type="number" min="0" max="27" bind:value={s.rebuild_when_days_left} /><span class="help">Extend the schedule automatically when fewer than this many days remain.</span></label>
          <label class="field">End of day overrun (minutes)<input type="number" min="0" bind:value={s.end_of_day_overrun_minutes} /><span class="help">How far past day end the last programme may run before filler is used instead.</span></label>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><h3>Era weights</h3></div>
        <p class="help small muted">Relative preference for programme years. Channels can override this.</p>
        <WeightRows value={s.era_weights} onchange={(v) => (s.era_weights = v)} keyLabel="Years" keyPlaceholder="1980-1989" addLabel="Add era" />
        <label class="field">Unknown year weight<input type="number" min="0" max="2" step="0.05" bind:value={s.unknown_year_weight} /><span class="help">Programmes with no year found still air at this weight; 0 excludes them.</span></label>
        <label class="field">Era pool normalisation <span class="mono">{Number(s.era_pool_normalise ?? 0).toFixed(2)}</span><input type="range" min="0" max="1" step="0.05" bind:value={s.era_pool_normalise} /><span class="help">0 weights every title equally; 1 makes each era's share of airtime follow the era weights regardless of how many titles it has.</span></label>
        <hr />
        <div class="card-title"><h3>Advert era weights</h3></div>
        <p class="help small muted">Adverts outside these years are never shown.</p>
        <WeightRows value={s.advert_era_weights} onchange={(v) => (s.advert_era_weights = v)} keyLabel="Years" keyPlaceholder="1980-1989" addLabel="Add era" />
        <hr />
        <div class="card-title"><h3>TV / movie balance</h3></div>
        <p class="help small muted">Global balance between episodes and films; dayparts and channels modify it.</p>
        <label class="field">TV <span class="mono">{Number(s.kind_weights.tv).toFixed(2)}</span><input type="range" min="0" max="1" step="0.05" bind:value={s.kind_weights.tv} /></label>
        <label class="field">Movie <span class="mono">{Number(s.kind_weights.movie).toFixed(2)}</span><input type="range" min="0" max="1" step="0.05" bind:value={s.kind_weights.movie} /></label>
      </div>

      <div class="card">
        <div class="card-title"><h3>Watershed</h3></div>
        <p class="help small muted">Earliest start time for films of each certificate.</p>
        <WeightRows value={s.watershed} onchange={(v) => (s.watershed = v)} keyLabel="Certificate" valueLabel="From" type="time" keyPlaceholder="15" addLabel="Add certificate" />
        <hr />
        <p class="help small muted">Earliest start time for TV episodes of each certificate (unlisted certificates are unrestricted).</p>
        <WeightRows value={s.tv_watershed} onchange={(v) => (s.tv_watershed = v)} keyLabel="Certificate" valueLabel="From" type="time" keyPlaceholder="18" addLabel="Add certificate" />
        <hr />
        <div class="form-grid">
          <label class="field">Unknown movie certificate
            <select bind:value={s.unknown_movie_certificate}>{#each CERTIFICATES as c (c)}<option value={c}>{c}</option>{/each}</select>
            <span class="help">Assumed for films with no certificate.</span></label>
          <label class="field">Unknown TV certificate
            <select bind:value={s.unknown_tv_certificate}>{#each CERTIFICATES as c (c)}<option value={c}>{c}</option>{/each}</select>
            <span class="help">Assumed for episodes with no certificate.</span></label>
          <label class="field">Kids cutoff<input type="time" bind:value={s.kids_cutoff} /><span class="help">Children's programmes are not scheduled after this time.</span></label>
          <label class="check"><input type="checkbox" bind:checked={s.weekend_kids_breakfast} /> Weekend kids' breakfast<span class="help">Boost children's programmes at breakfast on Saturday and Sunday.</span></label>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><h3>Variety and repeats</h3></div>
        <div class="stack">
          <label class="field">Movie repeat days<input type="number" min="0" bind:value={s.movie_repeat_days} /><span class="help">Minimum days before a film is shown again.</span></label>
          <label class="field">Show daily limit<input type="number" min="1" bind:value={s.show_daily_limit} /><span class="help">Maximum episodes of one show per channel per day.</span></label>
          <label class="field">Show repeat penalty<input type="number" step="0.05" min="0" max="1" bind:value={s.show_repeat_penalty} /><span class="help">Weight multiplier for each earlier airing of the same show that day.</span></label>
          <label class="field">Same slot bonus<input type="number" step="0.5" min="0" bind:value={s.same_slot_bonus} /><span class="help">Multiplier favouring a show at the time it aired yesterday, so regulars keep their slot.</span></label>
          <label class="field">Genre repeat penalty<input type="number" step="0.05" min="0" max="1" bind:value={s.genre_repeat_penalty} /><span class="help">Multiplier when the previous programme shared a genre.</span></label>
          <label class="field">Series rest weeks<input type="number" min="0" bind:value={s.series_rest_weeks} /><span class="help">Default weeks a series rests after its last episode before starting again.</span></label>
          <label class="check"><input type="checkbox" bind:checked={s.sport_back_to_back_weekends} /> Sport back to back at weekends<span class="help">Let sport programmes follow each other through weekend afternoons.</span></label>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><h3>Timing</h3></div>
        <div class="stack">
          <label class="field">Duration tolerance (minutes)<input type="number" min="0" bind:value={s.duration_tolerance_minutes} /><span class="help">How far a programme may overrun the gap it is chosen to fill.</span></label>
          <label class="field">Start rounding (minutes)<input type="number" min="1" bind:value={s.start_rounding_minutes} /><span class="help">Programme start times are rounded up to a multiple of this.</span></label>
        </div>
        <hr />
        <div class="card-title"><h3>Adverts</h3></div>
        <div class="stack">
          <label class="field">Advert year window<input type="number" min="0" bind:value={s.advert_year_window} /><span class="help">Prefer adverts from within this many years of the programme.</span></label>
          <label class="field">Advert repeat penalty (hours)<input type="number" min="0" bind:value={s.advert_repeat_penalty_hours} /><span class="help">Avoid repeating an advert within this many hours.</span></label>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><h3>Player</h3></div>
        <div class="stack">
          <label class="check"><input type="checkbox" bind:checked={s.nav_keys_change_channel} /> Up / down change channel<span class="help">When the guide is closed (the OSMC remote has no channel keys).</span></label>
          <label class="check"><input type="checkbox" bind:checked={s.nav_keys_change_volume} /> Left / right change volume<span class="help">When the guide is closed.</span></label>
          <label class="field">Channel badge seconds<input type="number" min="0" max="60" bind:value={s.badge_seconds} /><span class="help">How long the channel badge stays on screen after a change.</span></label>
          <label class="check"><input type="checkbox" bind:checked={s.channel_switch_static} /> Static burst on channel change</label>
          <label class="field">Pi hardware decoders<input class="mono" bind:value={s.pi_hwdec} /><span class="help">mpv <code>--hwdec</code> list tried in order on the Pi, e.g. drm-prime,v4l2m2m-copy.</span></label>
          <label class="field">Audio device<input class="mono" bind:value={s.audio_device} /><span class="help">mpv audio device name; <code>auto</code> picks the default output.</span></label>
          <label class="field">Overscan-safe margin<input type="number" min="0" max="0.2" step="0.01" bind:value={s.osd_safe_margin} /><span class="help">Fraction of each screen edge kept clear of overlays; a CRT hides about 5–8%.</span></label>
          <label class="field">On-screen text scale<input type="number" min="0.5" max="2" step="0.05" bind:value={s.osd_scale} /><span class="help">1.25 suits a 14" 4:3 set at 576 lines.</span></label>
          <label class="field">DRM connector<input class="mono" bind:value={s.drm_connector} /><span class="help">Force the output, e.g. <code>Composite-1</code> or <code>HDMI-A-1</code>; empty lets mpv choose.</span></label>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><h3>Cache</h3></div>
        <div class="stack">
          <label class="field">Cache directory<input class="mono" bind:value={s.cache_dir} placeholder="/mnt/cache/pitv" /><span class="help">Folder on the attached drive where pitv_content puts local copies of upcoming programmes; empty disables the cache.</span></label>
          <label class="field">Maximum size (GB)<input type="number" min="0" bind:value={s.cache_max_gb} /><span class="help">pitv_content fills the cache; PiTV only evicts under this cap.</span></label>
          <label class="field">Download directory<input class="mono" bind:value={s.acquire_dir} placeholder="(cache dir)/acquired" /><span class="help">Where pitv_content stores fetched wanted items, scanned as a library source; empty uses the cache directory.</span></label>
          <label class="field">pitv_content API URL<input class="mono" bind:value={s.content_tool_url} placeholder="http://127.0.0.1:8081" /><span class="help">Where pitv_content's local API listens.</span></label>
          <label class="check"><input type="checkbox" bind:checked={s.acquire_fill_gaps} /> Queue missing episodes automatically<span class="help">Looks for gaps between the episodes already on disk and adds them to the wanted list.</span></label>
          {#if s.content_profile}
            <div class="note small">Content profile: {s.content_profile.width}×{s.content_profile.height} {s.content_profile.vcodec}{s.content_profile.acodec ? `/${s.content_profile.acodec}` : ''}{s.content_profile.max_bitrate_kbps ? ` · ≤${s.content_profile.max_bitrate_kbps} kbit/s` : ''}{s.content_profile.deinterlace ? ` · deinterlace: ${s.content_profile.deinterlace}` : ''} <span class="muted">(what pitv_content transcodes to; read-only)</span></div>
          {/if}
        </div>
      </div>

      <div class="card">
        <div class="card-title"><h3>Maintenance</h3></div>
        <div class="stack">
          <label class="field">Nightly scan hour<input type="number" min="0" max="23" bind:value={s.scan_hour} /><span class="help">Hour of the day (0–23) when the library is rescanned and the schedule extended.</span></label>
          <label class="field">Keep history (days)<input type="number" min="1" bind:value={s.history_keep_days} /><span class="help">Airing history older than this is pruned.</span></label>
          <label class="field">Readiness check hours<ChipList value={(s.readiness_hours ?? []).map(String)} onchange={(v) => (s.readiness_hours = v.map((h) => parseInt(h, 10)).filter((h) => h >= 0 && h <= 23))} placeholder="hour 0–23…" /><span class="help">PiTV verifies tomorrow's files at these hours and substitutes anything missing.</span></label>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><h3>Music channel</h3></div>
      <p class="help small muted">A music channel's day is built from these blocks in time order: each block plays videos matching any of its genres and decades (empty = any); a concert block plays one full concert. The library counts on the right show what is actually available.</p>
      <div class="music">
        <div class="table-wrap">
          <table class="blocks">
            <thead><tr><th>Start</th><th>Name</th><th>Genres</th><th>Decades</th><th>Concert</th><th></th></tr></thead>
            <tbody>
              {#each s.music_blocks as b, i (i)}
                <tr>
                  <td><input type="time" bind:value={b.start} /></td>
                  <td><input bind:value={b.name} placeholder="Block name" /></td>
                  <td style="min-width:200px"><ChipList value={b.genres ?? []} onchange={(v) => (b.genres = v)} placeholder="genre…" lower /></td>
                  <td class="decades">{#each DECADES as d (d)}<label class="dec" class:on={(b.decades ?? []).includes(d)}><input type="checkbox" checked={(b.decades ?? []).includes(d)} onchange={() => toggleDecade(b, d)} />{d}s</label>{/each}</td>
                  <td class="center"><input type="checkbox" checked={!!b.concert} onchange={(e) => (b.concert = e.currentTarget.checked)} aria-label="Concert block" /></td>
                  <td class="nowrap"><button class="small ghost" disabled={i === 0} onclick={() => moveBlock(i, -1)} aria-label="Move up">↑</button><button class="small ghost" disabled={i === s.music_blocks.length - 1} onclick={() => moveBlock(i, 1)} aria-label="Move down">↓</button><button class="small ghost" onclick={() => s.music_blocks.splice(i, 1)} aria-label="Remove">✕</button></td>
                </tr>
              {:else}
                <tr><td colspan="6" class="muted small">No blocks: the music channel would be empty.</td></tr>
              {/each}
            </tbody>
          </table>
          <div class="mt"><button class="small" onclick={addBlock}>Add block</button></div>
        </div>
        <aside class="facets">
          <h4>In the library</h4>
          {#if facets}
            <div class="small"><b>{facets.concerts}</b> concerts</div>
            <div class="tiny muted mt" style="margin-top:.4rem">Decades</div>
            <div class="row tight">{#each Object.entries(facets.decades) as [d, n] (d)}<span class="chip">{d} <b>{n}</b></span>{:else}<span class="muted small">none</span>{/each}</div>
            <div class="tiny muted" style="margin-top:.4rem">Genres</div>
            <div class="row tight">{#each Object.entries(facets.genres) as [g, n] (g)}<span class="chip">{g} <b>{n}</b></span>{:else}<span class="muted small">none</span>{/each}</div>
          {:else}<div class="skeleton" style="height:60px"></div>{/if}
        </aside>
      </div>
      <div class="field mt"><span>Decades played</span>
        <div class="decades">{#each DECADES as d (d)}<label class="dec" class:on={(s.music_decades ?? []).includes(d)}><input type="checkbox" checked={(s.music_decades ?? []).includes(d)} onchange={() => toggleMusicDecade(d)} />{d}s</label>{/each}</div>
        <span class="help">The music channel only plays these decades.</span>
      </div>
      <div class="form-grid mt">
        <label class="field">Concert repeat (days)<input type="number" min="0" bind:value={s.music_concert_repeat_days} /><span class="help">Minimum days before the same concert is shown again.</span></label>
        <label class="field">Video repeat (hours)<input type="number" min="0" bind:value={s.music_video_repeat_hours} /><span class="help">Minimum hours before the same video is played again.</span></label>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><h3>Genres</h3></div>
      <div class="form-grid">
        <label class="field">Music genres<ChipList value={s.music_genres} onchange={(v) => (s.music_genres = v)} placeholder="add genre…" lower /><span class="help">Genre folder names recognised under a music source (matched case-insensitively).</span></label>
        <label class="field">Cartoon genres<ChipList value={s.cartoon_genres} onchange={(v) => (s.cartoon_genres = v)} placeholder="add genre…" lower /><span class="help">Shows with any of these genres are routed to a cartoons channel.</span></label>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><h3>Family-safe adverts</h3></div>
      <label class="field">Adult advert keywords<ChipList value={s.adult_advert_keywords} onchange={(v) => (s.adult_advert_keywords = v)} placeholder="add word…" lower /><span class="help">Adverts whose file name contains one of these words are flagged as not family-safe and never air on a channel with family-safe adverts on. Individual adverts can be overridden in Library → Adverts.</span></label>
    </div>

    <div class="card">
      <div class="card-title"><h3>Dayparts</h3></div>
      <p class="help small muted">Weights by time of day: TV, movie, kids and sport multipliers plus an optional maximum programme length. Weekdays, Saturday and Sunday each have their own table; channels may override any of them.</p>
      <h4>Weekday</h4>
      <DaypartTable bind:rows={s.dayparts} />
      <h4 class="mt">Saturday</h4>
      <DaypartTable bind:rows={s.dayparts_saturday} />
      <h4 class="mt">Sunday</h4>
      <DaypartTable bind:rows={s.dayparts_sunday} />
    </div>

    <div class="row"><button class="primary" onclick={save} disabled={saving}>Save settings</button></div>
  </div>
{:else}
  <div class="skeleton" style="height:300px"></div>
{/if}

<style>
  .music { display: grid; gap: 1rem; grid-template-columns: 1fr; }
  @media (min-width: 1000px) { .music { grid-template-columns: 1fr 260px; align-items: start; } }
  .blocks td { padding: .3rem .3rem; vertical-align: top; }
  .blocks input[type="time"] { min-width: 6.5rem; }
  .decades { min-width: 220px; display: flex; flex-wrap: wrap; gap: .2rem; }
  .dec { font-size: .72rem; padding: .1rem .4rem; border: 1px solid var(--border); border-radius: 999px; cursor: pointer; user-select: none; }
  .dec input { display: none; }
  .dec.on { background: var(--accent); color: #fff; border-color: var(--accent); }
  .facets { background: var(--bg-sunken); border-radius: var(--radius-sm); padding: .75rem; }
  .facets h4 { margin: 0 0 .4rem; font-size: .85rem; }
  .row.tight { gap: .25rem; }
  .row.tight .chip { font-size: .72rem; padding: .1rem .4rem; }
</style>
