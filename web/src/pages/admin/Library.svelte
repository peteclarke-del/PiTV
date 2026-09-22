<script>
  // The programme catalogue: everything PiTV can schedule, by kind, plus what was added here by
  // hand (custom programming pitv_content fetches). Each list loads whole; DataTable filters,
  // sorts and pages it in the browser.
  import { untrack } from 'svelte';
  import { del, get, post, put, tryApi, confirmApi } from '../../lib/api.js';
  import { changes, clock, route, toast } from '../../lib/stores.svelte.js';
  import { navigate } from '../../lib/router.js';
  import { fmtDuration, fmtAgo, fmtEpisode, isYouTube, lineupState, safeUrl, CERTIFICATES, WEEKDAYS } from '../../lib/format.js';
  import { num } from '../../lib/util.js';
  import { guard } from '../../lib/guard.svelte.js';
  import ChannelBadge from '../../components/ChannelBadge.svelte';
  import DataTable from '../../components/DataTable.svelte';
  import Tabs from '../../components/Tabs.svelte';
  import ShowEditor from './ShowEditor.svelte';
  import MediaEditor from './MediaEditor.svelte';
  import CatalogueCard from './CatalogueCard.svelte';
  import AddToCatalogue from './AddToCatalogue.svelte';
  import LineupEditor from './LineupEditor.svelte';
  import Availability from '../../components/Availability.svelte';
  import Codec from '../../components/Codec.svelte';

  const TABS = [
    { id: 'shows', label: 'Series', level: 'basic' }, { id: 'movies', label: 'Films', level: 'basic' },
    { id: 'custom', label: 'Added here', level: 'basic', title: 'Broadcast titles added by hand that pitv_content fetches; YouTube has its own list' },
    { id: 'youtube', label: 'Added YouTube', level: 'basic', title: 'Channels and playlists added by hand, whose videos become episodes' },
    { id: 'music', label: 'Music', level: 'standard' }, { id: 'adverts', label: 'Adverts', level: 'standard' },
    { id: 'idents', label: 'Idents', level: 'advanced' }, { id: 'attention', label: 'Needs attention', level: 'standard' },
  ];
  const KIND = { movies: 'movie', adverts: 'advert', idents: 'ident', music: 'music' };
  // The browser filters and sorts the whole list, so ask for all of it.
  const ALL = 20000;

  let tab = $derived(TABS.some((t) => t.id === route.parts[2]) ? route.parts[2] : 'shows');
  let channels = $state([]);
  let rows = $state(null);
  let showId = $state(null);
  let mediaId = $state(null);
  let lineupEntry = $state(null);
  let adding = $state(false);
  let fixes = $state({}); // Needs attention: pending quick-fix values by media id

  let chById = $derived(new Map(channels.map((c) => [c.id, c])));

  // Only the newest request may write: switching tabs quickly leaves older ones in flight.
  let seq = 0;
  async function load() {
    const t = tab, n = ++seq;
    if (!channels.length) channels = (await tryApi(get('/api/channels'))) ?? [];
    const r = await tryApi(t === 'shows' ? get('/api/shows')
      : t === 'attention' ? get('/api/library/attention')
      : t === 'custom' || t === 'youtube' ? get('/api/lineup')
      : get('/api/media', { kind: KIND[t], limit: ALL }));
    if (n !== seq) return;
    // The two added-by-hand lists divide the same rows between them: a creator's channel is not a
    // broadcast title and reads nothing like one, so neither list can be scanned while both are in it.
    rows = t === 'custom' ? (r ?? []).filter((e) => e.external && !isYouTube(e))
      : t === 'youtube' ? (r ?? []).filter((e) => e.external && isYouTube(e))
      : KIND[t] ? r?.items ?? [] : r ?? [];
  }
  $effect(() => { tab; changes.library; untrack(() => { rows = null; load(); }); });

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
  const removeCustom = (e) => confirmApi(`Remove ${e.title} from the catalogue? Its request to pitv_content is withdrawn.`,
    { title: 'Remove title', okLabel: 'Remove', danger: true }, () => del(`/api/lineup/${e.id}`), { success: 'Removed' }).then((r) => { if (r) load(); });
  // A new entry is shown in the list it belongs to, which is not always the one being looked at.
  function added(message, entry) {
    adding = false;
    toast.success(message);
    const want = isYouTube(entry) ? 'youtube' : 'custom';
    if (tab === want) load(); else navigate(`/admin/library/${want}`);
  }

  const title = { key: 'title', label: 'Title', cell: titleCell };
  const year = { key: 'year', label: 'Year' };
  const length = { key: 'duration', label: 'Length', class: 'small num', cell: lengthCell };
  const codec = { key: 'vcodec', label: 'Codec', class: 'small', cell: codecCell };
  const plays = { key: 'cached', label: 'Plays from', get: (m) => (m.cache_path ? 0 : m.origin === 'nas' ? 1 : 2), cell: availabilityCell };
  const flags = { key: 'flags', label: '', get: (m) => (m.attention ? 2 : m.excluded ? 1 : 0), cell: flagsCell };
  const channel = (get, cell) => ({ key: 'channel', label: 'Channel', get: (r) => chById.get(get(r))?.number, cell });
  let columns = $derived({
    shows: [
      { key: 'title', label: 'Title', cell: showTitleCell }, channel((s) => s.home_channel_id, homeChannelCell),
      { key: 'mode', label: 'Mode', class: 'small', get: modeLabel }, { key: 'certificate', label: 'Cert' },
      { key: 'year', label: 'Years', class: 'small', cell: yearsCell }, { key: 'episode_count', label: 'Eps', class: 'num' },
      { key: 'last_aired', label: 'Last aired', class: 'small muted', get: (s) => s.last_aired?.ts, cell: lastAiredCell },
    ],
    movies: [title, year, { key: 'certificate', label: 'Cert' }, length, codec, plays, channel((m) => m.home_channel_id, homeChannelCell), flags],
    music: [{ key: 'artist', label: 'Artist' }, { ...title, cell: musicTitleCell }, year,
      { key: 'genres', label: 'Genres', class: 'small', get: (m) => (m.genres ?? []).join(', ') }, length, codec, plays, flags],
    adverts: [title, year, length, codec, plays, { key: 'family_safe', label: 'Family-safe', cell: familySafeCell }, flags],
    idents: [title, length, codec, plays, channel((m) => m.home_channel_id, identChannelCell), flags],
    custom: [
      { key: 'title', label: 'Title', cell: customTitleCell }, { key: 'kind', label: 'Kind', get: (e) => (e.kind === 'show' ? 'series' : 'film') },
      year, channel((e) => e.channel_id, lineupChannelCell), { key: 'state', label: 'State', get: (e) => lineupState(e)[1], cell: stateCell },
      { key: 'transient', label: 'After airing', get: (e) => (e.transient ? 'removed' : 'kept') },
      { key: 'actions', label: '', class: 'right', sortable: false, cell: removeCell },
    ],
    youtube: [
      { key: 'title', label: 'Title', cell: youtubeTitleCell },
      { key: 'address', label: 'Channel or playlist', class: 'small', get: (e) => e.match?.id ?? '', cell: youtubeAddressCell },
      { key: 'keyed', label: 'Keyed on', class: 'small', get: (e) => (permanentId(e) ? 'permanent' : 'by name'), cell: youtubeKeyCell },
      channel((e) => e.channel_id, lineupChannelCell),
      { key: 'state', label: 'State', get: (e) => lineupState(e)[1], cell: stateCell },
      { key: 'actions', label: '', class: 'right', sortable: false, cell: removeCell },
    ],
    attention: [
      { key: 'title', label: 'Item', get: (i) => `${i.show_title ?? ''} ${i.title}`, cell: attentionItemCell },
      { key: 'attention', label: 'Reason', class: 'small' }, { key: 'fix', label: 'Quick fix', sortable: false, cell: fixCell },
    ],
  }[tab]);
  const EMPTY = {
    shows: 'No series in the catalogue.', movies: 'No films in the catalogue.', music: 'No music videos. Add a music source under pitv_content, Sources, then import the catalogue.',
    adverts: 'No adverts.', idents: 'No idents.', custom: 'Nothing added by hand yet: use Add to the catalogue.',
    youtube: 'No YouTube channels yet: use Add to the catalogue and choose YouTube channel.', attention: 'Nothing needs attention.',
  };
  // A handle belongs to the creator and stops resolving the day they change it. The channel's
  // own id never does, so an entry can be moved onto it while the handle still works.
  let fragile = $derived(tab === 'youtube' ? (rows ?? []).filter((e) => !permanentId(e)).length : 0);
  const permanentId = (e) => /^(UC[\w-]{22}|(PL|UU|FL|OL|RD)[\w-]{10,})$/.test(e?.match?.id ?? '');
  const resolveIds = guard(async () => {
    const r = await tryApi(post('/api/lineup/resolve-ids', {}));
    if (r) { toast.success(r.summary); load(); }
  });

  const edit = (r) => (tab === 'shows' ? (showId = r.id) : KIND[tab] ? (mediaId = r.id)
    : tab === 'custom' || tab === 'youtube' ? (lineupEntry = r) : undefined);
</script>

<div class="stack">
  <CatalogueCard />
  <div class="row">
    <Tabs tabs={TABS} active={tab} onselect={(id) => navigate(`/admin/library/${id}`)} label="Catalogue lists" />
    <span class="spacer"></span>
    {#if tab === 'youtube' && fragile}
      <button onclick={resolveIds} disabled={resolveIds.busy}
              title="Ask pitv_content for each channel's permanent id, while the handles still work">
        {resolveIds.busy ? 'Asking…' : `Re-key ${fragile} on permanent ids`}</button>
    {/if}
    <button class="primary" onclick={() => (adding = true)}>Add to the catalogue</button>
  </div>
  {#key tab}
    <DataTable id="catalogue-{tab}" {columns} {rows} search="Filter titles…" empty={EMPTY[tab]}
      onrow={tab === 'shows' || tab === 'custom' || tab === 'youtube' || KIND[tab] ? edit : null} rowClass={(r) => (r.excluded ? 'off' : '')} />
  {/key}
</div>

{#snippet titleCell(m)}<b>{m.title}</b><div class="tiny muted truncate" style="max-width:320px">{m.filename}</div>{/snippet}
{#snippet musicTitleCell(m)}<b>{m.title}</b>{#if m.concert}<span class="badge info">concert</span>{/if}<div class="tiny muted truncate" style="max-width:320px">{m.filename}</div>{/snippet}
{#snippet showTitleCell(s)}<b>{s.title}</b>{#if s.category === 'sport'}<span class="badge info">sport</span>{/if}{#if s.cartoon}<span class="badge info">cartoon</span>{:else if s.kids}<span class="badge info">children's</span>{/if}{#if s.excluded}<span class="badge">excluded</span>{/if}{#if s.attention_count}<span class="badge warn">{s.attention_count}</span>{/if}{/snippet}
{#snippet yearsCell(s)}{s.year ?? '?'}{s.end_year && s.end_year !== s.year ? `–${s.end_year}` : ''}{/snippet}
{#snippet lastAiredCell(s)}{#if s.last_aired}{fmtEpisode(s.last_aired.season, s.last_aired.episode)} · {fmtAgo(s.last_aired.ts, clock.ts)}{:else}never{/if}{/snippet}
{#snippet lengthCell(m)}{fmtDuration(m.duration)}{/snippet}
{#snippet codecCell(m)}<Codec item={m} />{/snippet}
{#snippet availabilityCell(m)}<Availability item={m} />{/snippet}
{#snippet flagsCell(m)}{#if m.excluded}<span class="badge">excluded</span>{/if}{#if m.attention}<span class="badge warn" title={m.attention}>!</span>{/if}{/snippet}
{#snippet badgeFor(id)}{#if chById.get(id)}<ChannelBadge channel={chById.get(id)} size="sm" name={false} />{:else}<span class="muted">–</span>{/if}{/snippet}
{#snippet homeChannelCell(r)}{@render badgeFor(r.home_channel_id)}{/snippet}
{#snippet lineupChannelCell(e)}{@render badgeFor(e.channel_id)}{/snippet}
{#snippet identChannelCell(m)}{#if chById.get(m.home_channel_id)}<ChannelBadge channel={chById.get(m.home_channel_id)} size="sm" />{:else}<span class="muted">any</span>{/if}{/snippet}
{#snippet familySafeCell(m)}<span role="presentation" onclick={(e) => e.stopPropagation()} onkeydown={(e) => e.stopPropagation()}><label class="check small" title={m.family_safe ? 'May air on family-safe channels' : 'Never airs on family-safe channels'}><input type="checkbox" checked={!!m.family_safe} onchange={(e) => setFamilySafe(m, e.currentTarget.checked)} />{m.family_safe ? 'yes' : 'no'}</label></span>{/snippet}
{#snippet customTitleCell(e)}<b>{e.title}</b>{#if e.match}{@const link = safeUrl(e.match.url)}<span class="badge ok" title="Confirmed online; pitv_content fetches this title">{#if link}<a href={link} target="_blank" rel="noopener noreferrer">{e.match.source} ↗</a>{:else}{e.match.source}{/if}</span>{:else}<span class="badge warn" title="Added without an online match; pitv_content searches by title">unmatched</span>{/if}{#if e.genres?.length}<div class="tiny muted">{e.genres.join(', ')}</div>{/if}{/snippet}
{#snippet youtubeTitleCell(e)}<b>{e.title}</b>{#if e.genres?.length}<div class="tiny muted">{e.genres.filter((g) => g !== 'YouTube').join(', ') || 'no subject yet'}</div>{/if}{/snippet}
{#snippet youtubeKeyCell(e)}{#if permanentId(e)}<span class="badge ok" title="The channel's own id: it survives a rename">permanent</span>{:else}<span class="badge warn" title="A handle or vanity name: it stops resolving if the creator changes it">by name</span>{/if}{/snippet}
{#snippet youtubeAddressCell(e)}{@const link = safeUrl(e.match?.url)}{#if link}<a href={link} target="_blank" rel="noopener noreferrer">{e.match.id} ↗</a>{:else}<span class="muted">{e.match?.id ?? '–'}</span>{/if}{/snippet}
{#snippet stateCell(e)}{@const [cls, text] = lineupState(e)}<span class="badge {cls}">{text}</span>{/snippet}
{#snippet removeCell(e)}<button class="small ghost" onclick={(event) => { event.stopPropagation(); removeCustom(e); }}>Remove</button>{/snippet}
{#snippet attentionItemCell(item)}<button class="ghost small" onclick={() => (mediaId = item.id)}><b>{item.show_title ? `${item.show_title} · ` : ''}{item.title}</b></button>
  <div class="tiny muted">{item.kind}{item.year ? ` · ${item.year}` : ''}{item.vcodec ? ` · ${item.vcodec}` : ''}</div>{/snippet}
{#snippet fixCell(item)}
  <div class="inline-form">
    <input class="xnarrow" type="number" placeholder="Year" aria-label="Year" min="1900" max="2100" value={fixes[item.id]?.year ?? ''} oninput={(e) => (fixes[item.id] = { ...fixes[item.id], year: e.currentTarget.value })} />
    <select value={fixes[item.id]?.certificate ?? ''} aria-label="Certificate" onchange={(e) => (fixes[item.id] = { ...fixes[item.id], certificate: e.currentTarget.value })}>
      <option value="">Cert</option>{#each CERTIFICATES as c (c)}<option value={c}>{c}</option>{/each}
    </select>
    <button class="small primary" onclick={() => fix(item)} disabled={fix.busy || (!fixes[item.id]?.year && !fixes[item.id]?.certificate)}>Save</button>
  </div>
{/snippet}

<AddToCatalogue open={adding} {channels} onclose={() => (adding = false)} onadded={added} />
{#if lineupEntry}
  <LineupEditor entry={lineupEntry} {channels} onclose={() => (lineupEntry = null)} onsaved={load} />
{/if}
{#if showId}
  <ShowEditor id={showId} {channels} onclose={() => (showId = null)} onsaved={load} />
{/if}
{#if mediaId}
  <MediaEditor id={mediaId} {channels} onclose={() => (mediaId = null)} onsaved={load} />
{/if}
