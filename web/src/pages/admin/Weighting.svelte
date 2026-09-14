<script>
  import { onMount } from 'svelte';
  import { get, put, post, tryApi } from '../../lib/api.js';
  import { confirm, toast } from '../../lib/stores.svelte.js';
  import { CERTIFICATES } from '../../lib/format.js';
  import WeightRows from './WeightRows.svelte';
  import DaypartTable from './DaypartTable.svelte';

  let s = $state(null);
  let saving = $state(false);
  let savedOnce = $state(false);

  let providers = $state({ archive: true, url: false });
  async function load() {
    s = await tryApi(get('/api/settings'));
    if (s) providers = { archive: (s.acquire_providers ?? []).includes('archive'), url: (s.acquire_providers ?? []).includes('url') };
  }
  onMount(load);

  async function save() {
    saving = true;
    const body = { ...s };
    for (const k of ['horizon_days', 'rebuild_when_days_left', 'show_daily_limit', 'movie_repeat_days', 'episode_recency_days',
                     'duration_tolerance_minutes', 'start_rounding_minutes', 'end_of_day_overrun_minutes', 'advert_year_window',
                     'advert_repeat_penalty_hours', 'series_rest_weeks', 'badge_seconds', 'cache_max_gb', 'cache_copy_mbps',
                     'prefetch_hours', 'transcode_max_height', 'transcode_bitrate_kbps', 'scan_hour', 'history_keep_days']) body[k] = parseInt(body[k], 10) || 0;
    body.acquire_providers = ['archive', 'url'].filter((p) => providers[p]);
    for (const k of ['show_repeat_penalty', 'same_slot_bonus', 'genre_repeat_penalty']) body[k] = Number(body[k]) || 0;
    body.kind_weights = { tv: Number(s.kind_weights.tv), movie: Number(s.kind_weights.movie) };
    body.dayparts = s.dayparts.map((d) => ({ ...d, tv: Number(d.tv), movie: Number(d.movie), kids: Number(d.kids), ...(d.max_minutes ? { max_minutes: Number(d.max_minutes) } : {}) }))
      .map((d) => { if (!d.max_minutes) delete d.max_minutes; return d; });
    const r = await tryApi(put('/api/settings', body), { success: 'Settings saved' });
    saving = false;
    if (r) { s = r; savedOnce = true; providers = { archive: (s.acquire_providers ?? []).includes('archive'), url: (s.acquire_providers ?? []).includes('url') }; }
  }
  async function reset() {
    if (!(await confirm('Reset every weighting setting to its default?', { title: 'Reset to defaults', okLabel: 'Reset', danger: true }))) return;
    const r = await tryApi(post('/api/settings/reset', {}), { success: 'Settings reset' });
    if (r) { s = r; savedOnce = true; }
  }
  const build = () => tryApi(post('/api/schedule/build', {}), { success: 'Schedule build started' }).then(() => toast.info('Changes apply to newly built days; use "Rebuild week" on the dashboard to redo existing days.'));
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
          <label class="field">Episode recency days<input type="number" min="0" bind:value={s.episode_recency_days} /><span class="help">Episodes aired within this many days are avoided.</span></label>
          <label class="field">Show daily limit<input type="number" min="1" bind:value={s.show_daily_limit} /><span class="help">Maximum episodes of one show per channel per day.</span></label>
          <label class="field">Show repeat penalty<input type="number" step="0.05" min="0" max="1" bind:value={s.show_repeat_penalty} /><span class="help">Weight multiplier for each earlier airing of the same show that day.</span></label>
          <label class="field">Same slot bonus<input type="number" step="0.5" min="0" bind:value={s.same_slot_bonus} /><span class="help">Multiplier favouring a show at the time it aired yesterday, so regulars keep their slot.</span></label>
          <label class="field">Genre repeat penalty<input type="number" step="0.05" min="0" max="1" bind:value={s.genre_repeat_penalty} /><span class="help">Multiplier when the previous programme shared a genre.</span></label>
          <label class="field">Series rest weeks<input type="number" min="0" bind:value={s.series_rest_weeks} /><span class="help">Default weeks a series rests after its last episode before starting again.</span></label>
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
          <label class="field">Audio device<input class="mono" bind:value={s.audio_device} /><span class="help">mpv audio device name; <code>auto</code> picks HDMI.</span></label>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><h3>Cache</h3></div>
        <div class="stack">
          <label class="field">Cache directory<input class="mono" bind:value={s.cache_dir} placeholder="/mnt/cache/pitv" /><span class="help">Folder on the attached drive for local copies of upcoming programmes; empty disables the cache.</span></label>
          <label class="field">Maximum size (GB)<input type="number" min="0" bind:value={s.cache_max_gb} /><span class="help">Oldest copies are removed once the cache exceeds this.</span></label>
          <label class="field">Copy speed limit (Mbit/s)<input type="number" min="0" bind:value={s.cache_copy_mbps} /><span class="help">0 = unlimited. Throttle to keep the NAS responsive while a programme plays.</span></label>
          <label class="field">Prefetch hours<input type="number" min="0" max="48" bind:value={s.prefetch_hours} /><span class="help">Copy programmes scheduled within this many hours ahead.</span></label>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><h3>Acquisition</h3></div>
        <div class="stack">
          <label class="check"><input type="checkbox" bind:checked={s.acquire_enabled} /> Fetch wanted programmes<span class="help">Downloads items from the Acquire page in the background.</span></label>
          <label class="field">Download directory<input class="mono" bind:value={s.acquire_dir} placeholder="(cache dir)/acquired" /><span class="help">Where downloaded files are stored and scanned from; empty uses the cache directory.</span></label>
          <div class="field"><span>Providers</span>
            <div class="row"><label class="check"><input type="checkbox" bind:checked={providers.archive} /> archive.org</label><label class="check"><input type="checkbox" bind:checked={providers.url} /> Direct URLs</label></div>
            <span class="help">Only archive.org searches and explicit URLs are supported.</span>
          </div>
          <label class="check"><input type="checkbox" bind:checked={s.acquire_fill_gaps} /> Queue missing episodes automatically<span class="help">Looks for gaps between the episodes already on disk.</span></label>
          <label class="field">Acquisition hours<input class="mono" style="width:9rem" bind:value={s.acquire_hours} placeholder="00:00-23:59" /><span class="help">Window (HH:MM-HH:MM) in which downloads may run.</span></label>
          <label class="check"><input type="checkbox" bind:checked={s.transcode_enabled} /> Transcode software-decoded files<span class="help">Re-encode to H.264 so the Pi can hardware-decode them.</span></label>
          <label class="field">Transcode hours<input class="mono" style="width:9rem" bind:value={s.transcode_hours} placeholder="01:00-07:00" /><span class="help">Window (HH:MM-HH:MM) for CPU-heavy transcoding, ideally overnight.</span></label>
          <label class="field">Max height (pixels)<input type="number" min="240" max="2160" step="1" bind:value={s.transcode_max_height} /><span class="help">Taller sources are scaled down, e.g. 720.</span></label>
          <label class="field">Bitrate (kbit/s)<input type="number" min="500" bind:value={s.transcode_bitrate_kbps} /><span class="help">Target video bitrate for transcoded copies.</span></label>
        </div>
      </div>

      <div class="card">
        <div class="card-title"><h3>Maintenance</h3></div>
        <div class="stack">
          <label class="field">Nightly scan hour<input type="number" min="0" max="23" bind:value={s.scan_hour} /><span class="help">Hour of the day (0–23) when the library is rescanned and the schedule extended.</span></label>
          <label class="field">Keep history (days)<input type="number" min="1" bind:value={s.history_keep_days} /><span class="help">Airing history older than this is pruned.</span></label>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-title"><h3>Dayparts</h3></div>
      <p class="help small muted">Weights by time of day: TV and movie multipliers, a kids multiplier, and an optional maximum programme length. Channels may define their own profile.</p>
      <DaypartTable bind:rows={s.dayparts} />
    </div>

    <div class="row"><button class="primary" onclick={save} disabled={saving}>Save settings</button></div>
  </div>
{:else}
  <div class="skeleton" style="height:300px"></div>
{/if}
