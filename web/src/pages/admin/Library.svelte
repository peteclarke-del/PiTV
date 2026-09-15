<script>
  import { untrack } from 'svelte';
  import { get, put, tryApi } from '../../lib/api.js';
  import { changes, clock, route } from '../../lib/stores.svelte.js';
  import { navigate } from '../../lib/router.js';
  import { fmtDuration, fmtAgo, fmtEpisode, CERTIFICATES, WEEKDAYS } from '../../lib/format.js';
  import { debounce, onEnter, num } from '../../lib/util.js';
  import { guard } from '../../lib/guard.svelte.js';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
  import ShowEditor from './ShowEditor.svelte';
  import MediaEditor from './MediaEditor.svelte';
  import CatalogueCard from './CatalogueCard.svelte';
  import Availability from '../../components/Availability.svelte';
  import Codec from '../../components/Codec.svelte';

  const TABS = [['shows', 'Shows'], ['movies', 'Movies'], ['music', 'Music'], ['adverts', 'Adverts'], ['idents', 'Idents'], ['attention', 'Needs attention']];
  const KIND = { movies: 'movie', adverts: 'advert', idents: 'ident', music: 'music' };
  const PAGE = 50;

  let tab = $derived(route.parts[2] ?? 'shows');
  let q = $state('');
  let channels = $state([]);
  let shows = $state(null);
  let media = $state(null); // {total, items}
  let offset = $state(0);
  let attention = $state(null);
  let showId = $state(null);
  let mediaId = $state(null);
  let fixes = $state({}); // attention tab: pending quick-fix values by media id

  let chById = $derived(new Map(channels.map((c) => [c.id, c])));

  // Only the newest request may write: switching tabs or typing quickly leaves older ones in flight.
  let seq = 0;
  async function load() {
    const t = tab, n = ++seq;
    if (!channels.length) channels = (await tryApi(get('/api/channels'))) ?? [];
    const r = await tryApi(t === 'shows' ? get('/api/shows', { q })
      : t === 'attention' ? get('/api/library/attention')
      : get('/api/media', { kind: KIND[t], q, limit: PAGE, offset }));
    if (n !== seq) return;
    if (t === 'shows') shows = r ?? shows ?? [];
    else if (t === 'attention') attention = r ?? attention ?? [];
    else media = r ?? media;
  }
  $effect(() => { tab; changes.library; offset; untrack(load); });
  // Resetting a non-zero offset refetches through the effect above.
  const reload = debounce(() => { if (offset) offset = 0; else load(); }, 250);
  function search(v) { q = v; reload(); }
  function switchTab(id) {
    q = ''; offset = 0;
    navigate(`/admin/library/${id}`);
  }
  function modeLabel(s) {
    if (s.mode === 'auto') return 'auto';
    const days = (s.anchor_days ?? []).map((d) => WEEKDAYS[d]?.slice(0, 2)).join('');
    return `${s.mode} ${s.anchor_time ?? ''} ${days}`.trim();
  }
  async function setFamilySafe(m, v) {
    if (await tryApi(put(`/api/media/${m.id}`, { family_safe: v }), { success: v ? 'Marked family-safe' : 'Marked not family-safe' })) m.family_safe = v ? 1 : 0;
  }
  const fix = guard(async (item) => {
    const f = fixes[item.id] ?? {};
    const body = {};
    const year = num(f.year, { min: 1900, max: 2100, int: true });
    if (year !== null) body.year = year;
    if (f.certificate) body.certificate = f.certificate;
    if (!Object.keys(body).length) return;
    if (await tryApi(put(`/api/media/${item.id}`, body), { success: 'Saved' })) { delete fixes[item.id]; load(); }
  });
</script>

<div class="stack">
  <CatalogueCard />
  <nav class="tabs sub">
    {#each TABS as [id, label] (id)}
      <button class:active={tab === id} onclick={() => switchTab(id)}>{label}</button>
    {/each}
  </nav>

  {#if tab !== 'attention'}
    <div class="row">
      <input type="search" placeholder="Search titles…" aria-label="Search titles" value={q} oninput={(e) => search(e.currentTarget.value)} style="flex:1;max-width:360px" />
      {#if tab !== 'shows' && media}
        <span class="small muted">{media.total} items</span>
        <span class="btn-group">
          <button class="small" disabled={offset === 0} onclick={() => (offset = Math.max(0, offset - PAGE))}>Prev</button>
          <button class="small" disabled={offset + PAGE >= media.total} onclick={() => (offset += PAGE)}>Next</button>
        </span>
      {:else if shows}
        <span class="small muted">{shows.length} shows</span>
      {/if}
    </div>
  {/if}

  {#if tab === 'shows'}
    <div class="card pad-0 table-wrap">
      <table>
        <thead><tr><th>Title</th><th>Channel</th><th>Mode</th><th>Cert</th><th>Years</th><th class="num">Eps</th><th>Last aired</th></tr></thead>
        <tbody>
          {#each shows ?? [] as s (s.id)}
            <tr class="clickable" tabindex="0" onclick={() => (showId = s.id)} onkeydown={onEnter(() => (showId = s.id))}>
              <td><b>{s.title}</b>{#if s.category && s.category !== 'general'}<span class="badge info">{s.category}</span>{/if}{#if s.excluded}<span class="badge">excluded</span>{/if}{#if s.attention_count}<span class="badge warn">{s.attention_count}</span>{/if}</td>
              <td>{#if chById.get(s.home_channel_id)}<ChannelBadge channel={chById.get(s.home_channel_id)} size="sm" name={false} />{:else}<span class="muted">–</span>{/if}</td>
              <td class="small">{modeLabel(s)}</td>
              <td>{s.certificate ?? '–'}</td>
              <td class="small">{s.year ?? '?'}{s.end_year && s.end_year !== s.year ? `–${s.end_year}` : ''}</td>
              <td class="num">{s.episode_count}</td>
              <td class="small muted">{#if s.last_aired}{fmtEpisode(s.last_aired.season, s.last_aired.episode)} · {fmtAgo(s.last_aired.ts, clock.ts)}{:else}never{/if}</td>
            </tr>
          {:else}
            <tr><td colspan="7" class="empty">{shows ? 'No shows found.' : 'Loading…'}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if tab === 'attention'}
    <div class="card pad-0 table-wrap">
      <table>
        <thead><tr><th>Item</th><th>Reason</th><th>Quick fix</th></tr></thead>
        <tbody>
          {#each attention ?? [] as item (item.id)}
            <tr>
              <td><button class="ghost small" onclick={() => (mediaId = item.id)}><b>{item.show_title ? `${item.show_title} · ` : ''}{item.title}</b></button>
                <div class="tiny muted">{item.kind}{item.year ? ` · ${item.year}` : ''}{item.vcodec ? ` · ${item.vcodec}` : ''}</div></td>
              <td class="small">{item.attention}</td>
              <td>
                <div class="inline-form">
                  <input class="xnarrow" type="number" placeholder="Year" aria-label="Year" min="1900" max="2100" value={fixes[item.id]?.year ?? ''} oninput={(e) => (fixes[item.id] = { ...fixes[item.id], year: e.currentTarget.value })} />
                  <select value={fixes[item.id]?.certificate ?? ''} aria-label="Certificate" onchange={(e) => (fixes[item.id] = { ...fixes[item.id], certificate: e.currentTarget.value })}>
                    <option value="">Cert</option>{#each CERTIFICATES as c (c)}<option value={c}>{c}</option>{/each}
                  </select>
                  <button class="small primary" onclick={() => fix(item)} disabled={fix.busy || (!fixes[item.id]?.year && !fixes[item.id]?.certificate)}>Save</button>
                </div>
              </td>
            </tr>
          {:else}
            <tr><td colspan="3" class="empty">{attention ? 'Nothing needs attention.' : 'Loading…'}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if tab === 'music'}
    <div class="card pad-0 table-wrap">
      <table>
        <thead><tr><th>Artist</th><th>Title</th><th>Year</th><th>Genres</th><th>Length</th><th>Codec</th><th>Plays from</th><th></th></tr></thead>
        <tbody>
          {#each media?.items ?? [] as m (m.id)}
            <tr class="clickable" tabindex="0" onclick={() => (mediaId = m.id)} onkeydown={onEnter(() => (mediaId = m.id))}>
              <td>{m.artist ?? '–'}</td>
              <td><b>{m.title}</b>{#if m.concert}<span class="badge info">concert</span>{/if}<div class="tiny muted truncate" style="max-width:320px">{m.filename}</div></td>
              <td>{m.year ?? '–'}</td>
              <td class="small">{(m.genres ?? []).join(', ') || '–'}</td>
              <td class="small">{fmtDuration(m.duration)}</td>
              <td class="small"><Codec item={m} /></td>
              <td><Availability item={m} /></td>
              <td>{#if m.excluded}<span class="badge">excluded</span>{/if}{#if m.attention}<span class="badge warn" title={m.attention}>!</span>{/if}</td>
            </tr>
          {:else}
            <tr><td colspan="8" class="empty">{media ? 'No music videos in the catalogue. Add a music source in pitv_content Sources, then import the catalogue.' : 'Loading…'}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else}
    <div class="card pad-0 table-wrap">
      <table>
        <thead><tr><th>Title</th><th>Year</th><th>Cert</th><th>Length</th><th>Codec</th><th>Plays from</th>{#if tab === 'idents'}<th>Channel</th>{/if}{#if tab === 'adverts'}<th>Family-safe</th>{/if}<th></th></tr></thead>
        <tbody>
          {#each media?.items ?? [] as m (m.id)}
            <tr class="clickable" tabindex="0" onclick={() => (mediaId = m.id)} onkeydown={onEnter(() => (mediaId = m.id))}>
              <td><b>{m.title}</b><div class="tiny muted truncate" style="max-width:320px">{m.filename}</div></td>
              <td>{m.year ?? '–'}</td>
              <td>{m.certificate ?? '–'}</td>
              <td class="small">{fmtDuration(m.duration)}</td>
              <td class="small"><Codec item={m} /></td>
              <td><Availability item={m} /></td>
              {#if tab === 'idents'}<td>{#if chById.get(m.home_channel_id)}<ChannelBadge channel={chById.get(m.home_channel_id)} size="sm" />{:else}<span class="muted">any</span>{/if}</td>{/if}
              {#if tab === 'adverts'}<td onclick={(e) => e.stopPropagation()}><label class="check small" title={m.family_safe ? 'Family-safe: may air on family-safe channels' : 'Not family-safe: never airs on family-safe channels'}><input type="checkbox" checked={!!m.family_safe} onchange={(e) => setFamilySafe(m, e.currentTarget.checked)} />{m.family_safe ? 'yes' : 'no'}</label></td>{/if}
              <td>{#if m.excluded}<span class="badge">excluded</span>{/if}{#if m.attention}<span class="badge warn" title={m.attention}>!</span>{/if}</td>
            </tr>
          {:else}
            <tr><td colspan="9" class="empty">{media ? 'No items found.' : 'Loading…'}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</div>

{#if showId}
  <ShowEditor id={showId} {channels} onclose={() => (showId = null)} onsaved={load} />
{/if}
{#if mediaId}
  <MediaEditor id={mediaId} {channels} onclose={() => (mediaId = null)} onsaved={load} />
{/if}
