<script>
  // Everything the two applications know to be wrong with themselves, in one place. The report
  // has existed as `pitv doctor` since the beginning but had no page, so every finding it raised
  // was visible only from a terminal: a cache too small for the schedule, a queue that healed
  // itself, a channel that cannot reach its configured mix. A finding nobody sees is no better
  // than silence, which is the thing the report exists to prevent.
  import { onMount } from 'svelte';
  import { get, post, tryApi } from '../../lib/api.js';
  import { fmtBytes, fmtDateTime } from '../../lib/format.js';
  import { poll } from '../../lib/poll.svelte.js';

  let doc = $state(null);
  let error = $state('');
  let busy = $state(false);

  async function load() {
    busy = true;
    try { doc = await get('/api/doctor'); error = ''; }
    catch (e) { error = e.detail || e.message; }
    busy = false;
  }
  onMount(load);
  poll(load, 60000);

  const findings = $derived(doc?.findings ?? []);
  // A finding that names something already put right reads differently from one still wrong.
  const healed = (f) => f.startsWith('pitv_content healed');

  // pitv_content refuses (409) while a job runs, since that job's files look the same.
  let cleaning = $state(false);
  async function cleanup() {
    cleaning = true;
    await tryApi(post('/api/content/tool/api/cleanup'), { success: 'Cleaned up' });
    cleaning = false;
    await load();
  }

  // A band rested because pitv_content's searches for it found nothing: asking again clears the
  // rest and asks at once, which is worth doing after widening its genres or years.
  let asking = $state(null);
  async function askAgain(id) {
    asking = id;
    await tryApi(post(`/api/bands/${id}/ask`), { success: 'Asked again' });
    asking = null;
    await load();
  }

  function gib(n) { return typeof n === 'number' ? fmtBytes(n) : '-'; }
</script>

<section>
  <header class="head">
    <h2>Doctor</h2>
    <button class="small" onclick={load} disabled={busy}>{busy ? 'Checking…' : 'Check again'}</button>
  </header>

  {#if error}
    <p class="error">The report could not be gathered: {error}</p>
  {:else if !doc}
    <p class="muted">Checking…</p>
  {:else}
    {#if findings.length === 0}
      <p class="ok">Nothing needs attention.</p>
    {:else}
      <ul class="findings">
        {#each findings as f (f)}
          <li class:healed={healed(f)}>{f}</li>
        {/each}
      </ul>
    {/if}

    <div class="cards">
      <article>
        <h3>Cache</h3>
        <dl>
          <dt>Holding</dt><dd>{gib(doc.cache?.usage?.used)} of {gib(doc.cache?.usage?.max)}</dd>
          <dt>Drive spare</dt><dd>{gib(doc.cache?.usage?.free)}</dd>
          {#if doc.cache?.cap_holds_days != null}
            <dt>Holds</dt><dd>{doc.cache.cap_holds_days} days of schedule</dd>
            <dt>Left for kept episodes</dt>
            <dd>{gib(doc.cache.room_for_kept_bytes)}, holding {gib(doc.cache.kept_bytes)}</dd>
          {/if}
          <dt>Next day cached</dt>
          <dd>{doc.cache?.cached ?? 0} of {doc.cache?.next_day_files ?? 0} ({doc.cache?.cached_percent ?? 0}%)</dd>
        </dl>
      </article>

      <article>
        <h3>pitv_content</h3>
        {#if doc.content?.reachable === false}
          <p class="error">Not reachable: {doc.content.detail}</p>
        {:else}
          <dl>
            <dt>State</dt><dd>{doc.content?.state ?? '-'}{doc.content?.idle_reason ? ` (${doc.content.idle_reason})` : ''}</dd>
            <dt>Working on</dt><dd>{doc.content?.active_job?.mode ?? 'nothing'}</dd>
            <dt>Queued</dt>
            <dd>{Object.entries(doc.content?.queued_by_mode ?? {}).map(([m, n]) => `${n} ${m}`).join(', ') || 'nothing'}</dd>
          </dl>
          {#if doc.content?.leftovers}
            <h4>Left by a stopped run</h4>
            <ul class="healed">
              {#each doc.content.leftovers.items ?? [] as l (l.name)}<li>{l.name}: {l.kind}, {l.mb} MB, {l.hours_old} h old</li>{/each}
            </ul>
            <button class="small" onclick={cleanup} disabled={cleaning}>{cleaning ? 'Cleaning up…' : 'Clean up'}</button>
          {/if}
          {#if doc.content?.healed?.length}
            <h4>Put right by itself</h4>
            <ul class="healed">{#each doc.content.healed as h (h)}<li>{h}</li>{/each}</ul>
          {/if}
        {/if}
      </article>

      {#if doc.bands?.rested?.length}
        <article>
          <h3>Bands resting</h3>
          <ul class="healed">
            {#each doc.bands.rested as r (r.id)}
              <li>
                {r.channel} / {r.band}: searched {r.searched}, found {r.made} of {r.count}; until {fmtDateTime(r.until)}
                <button class="small" onclick={() => askAgain(r.id)} disabled={asking === r.id}>
                  {asking === r.id ? 'Asking…' : 'Ask again now'}
                </button>
              </li>
            {/each}
          </ul>
        </article>
      {/if}

      <article>
        <h3>Schedule</h3>
        <dl>
          <dt>Built ahead</dt><dd>{doc.schedule?.days_ahead ?? 0} days</dd>
          <dt>Player</dt>
          <dd>{doc.player?.online ? `${doc.player.channel ?? ''} ${doc.player.showing ?? ''}`.trim() || 'on' : 'offline'}</dd>
          <dt>Report taken</dt><dd>{fmtDateTime(doc.generated_ts)}</dd>
        </dl>
      </article>
    </div>
  {/if}
</section>

<style>
  .head { display: flex; align-items: baseline; gap: 1rem; }
  .head h2 { flex: 1; }
  .findings { list-style: none; padding: 0; margin: 0 0 1.5rem; }
  .findings li {
    padding: 0.6rem 0.8rem; margin-bottom: 0.4rem; border-left: 3px solid var(--warn, #c88);
    background: var(--panel, rgba(255, 255, 255, 0.04)); border-radius: 0 4px 4px 0;
  }
  .findings li.healed { border-left-color: var(--ok, #6a6); opacity: 0.85; }
  .ok { color: var(--ok, #6a6); }
  .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(16rem, 1fr)); gap: 1rem; }
  .cards article { background: var(--panel, rgba(255, 255, 255, 0.04)); padding: 0.8rem 1rem; border-radius: 6px; }
  .cards h3 { margin-top: 0; }
  .cards h4 { margin-bottom: 0.3rem; font-size: 0.9em; }
  dl { display: grid; grid-template-columns: auto 1fr; gap: 0.2rem 0.8rem; margin: 0; }
  dt { color: var(--muted, #999); }
  dd { margin: 0; }
  .healed { margin: 0; padding-left: 1.1rem; font-size: 0.92em; }
</style>
