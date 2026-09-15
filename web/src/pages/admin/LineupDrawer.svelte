<script>
  // A channel's line-up: the series and films it carries, including material pitv_content still has to fetch.
  import AppBadge from '../../components/AppBadge.svelte';
  import { onMount } from 'svelte';
  import { get, post, put, del, tryApi, confirmApi } from '../../lib/api.js';
  import { hasLineup, lineupState } from '../../lib/format.js';
  import { debounce } from '../../lib/util.js';
  import DataTable from '../../components/DataTable.svelte';
  import Drawer from '../../components/Drawer.svelte';

  // Channels mounts a fresh drawer per channel, so `channel` is fixed for this instance.
  let { channel, channels = [], onclose, onchanged } = $props();
  let entries = $state(null);
  let q = $state('');
  let options = $state([]);
  let searching = $state(false);
  let manual = $state(null);   // {title, year, kind, episode_minutes, transient}
  let busy = $state(false);

  async function fetchEntries() {
    entries = (await tryApi(get('/api/lineup', { channel_id: channel.id }))) ?? entries ?? [];
  }
  // After an edit: refresh this list and the parent's per-channel counts.
  function afterEdit() {
    fetchEntries();
    onchanged?.();
  }
  onMount(fetchEntries);
  const searchSoon = debounce(async () => {
    searching = true;
    options = (await tryApi(get('/api/lineup/options', { q, limit: 30 }))) ?? [];
    searching = false;
  }, 250);
  function search(v) { q = v; searchSoon(); }
  async function addOption(o) {
    const body = { channel_id: channel.id };
    if (o.show_id) body.show_id = o.show_id; else if (o.media_id) body.media_id = o.media_id;
    else Object.assign(body, { title: o.title, year: o.year ?? null, kind: 'show', catalogue: !!o.catalogue });
    busy = true;
    const r = await tryApi(post('/api/lineup', body), { success: `${o.title} added${o.channel_id && o.channel_id !== channel.id ? ` (moved from Ch ${o.channel_number})` : ''}` });
    busy = false;
    if (r) { q = ''; options = []; afterEdit(); }
  }
  async function addManual() {
    const body = { channel_id: channel.id, title: manual.title.trim(), year: manual.year === '' ? null : Number(manual.year), kind: manual.kind, transient: manual.transient,
                   episode_minutes: manual.kind === 'show' && manual.episode_minutes !== '' ? Number(manual.episode_minutes) : null };
    busy = true;
    const r = await tryApi(post('/api/lineup', body), { success: `${body.title} added; pitv_content will look for it` });
    busy = false;
    if (r) { manual = null; afterEdit(); }
  }
  async function setField(e, field, value) {
    const r = await tryApi(put(`/api/lineup/${e.id}`, { [field]: value }));
    if (r) afterEdit();
  }
  async function move(e, channelId) {
    const target = channels.find((c) => c.id === Number(channelId));
    if (!target || target.id === channel.id) return;
    if (await tryApi(put(`/api/lineup/${e.id}`, { channel_id: target.id }), { success: `${e.title} moved to ${target.name}` })) afterEdit();
  }
  async function remove(e) {
    if (await confirmApi(`Remove "${e.title}" from ${channel.name}? It will have no channel until the generator or you place it again.`,
      { title: 'Remove from line-up', okLabel: 'Remove', danger: true }, () => del(`/api/lineup/${e.id}`))) afterEdit();
  }
  let others = $derived(channels.filter((c) => c.id !== channel.id && c.enabled && hasLineup(c)));
  const kindLabel = (e) => (e.kind === 'movie' ? 'film' : 'series');
  // No default sort: the API orders entries by kind, then title.
  const columns = [
    { key: 'title', label: 'Title', cell: titleCell },
    { key: 'kind', label: 'Kind', get: kindLabel, cell: kindCell },
    { key: 'state', label: 'State', get: (e) => lineupState(e)[1], cell: stateCell },
    { key: 'enabled', label: 'On', title: 'Enabled', class: 'control', get: (e) => !!e.enabled, cell: flag },
    { key: 'transient', label: 'Trans.', title: 'Transient: fetched files live only in the cache', get: (e) => !!e.transient, class: 'control', cell: flag },
    { key: 'remove_after_airing', label: 'Rm. after', title: 'Remove from the line-up after it airs', get: (e) => !!e.remove_after_airing, class: 'control', cell: flag },
    { key: 'move', label: 'Move to', sortable: false, cell: moveCell },
    { key: 'actions', label: '', class: 'right control', sortable: false, cell: actionsCell },
  ];
</script>

<Drawer open={true} title={`Line-up: ${channel.name}`} subtitle={`Channel ${channel.number} carries these series and films; each can be on one channel only`} {onclose} wide>
  <div class="stack">
    <p class="scope" style="margin:0"><AppBadge app="pitv" /> The line-up is PiTV's: it decides what this channel may schedule. Entries not on disk are requested from pitv_content, which fetches them into the cache.</p>
    <div class="add">
      <input type="search" placeholder="Add a series or film… (library and pitv_content catalogue)" value={q} oninput={(e) => search(e.currentTarget.value)} />
      {#if q}
        <ul class="opts">
          {#each options as o (`${o.type}-${o.show_id ?? o.media_id ?? o.title}`)}
            <li>
              <button class="ghost item" onclick={() => addOption(o)} disabled={busy || o.channel_id === channel.id}>
                <span class="truncate"><b>{o.title}</b> <span class="muted small">{o.year ?? ''}</span> <span class="badge">{o.type === 'movie' ? 'film' : 'series'}</span>{#if o.episodes}<span class="tiny muted"> · {o.episodes} eps</span>{/if}</span>
                <span class="tiny">{#if o.channel_id === channel.id}<span class="muted">already here</span>{:else if o.channel_id}<span class="badge warn">on Ch {o.channel_number}, will move</span>{:else if o.catalogue && !o.on_disk}<span class="badge info">not on disk, via pitv_content</span>{:else if !o.on_disk}<span class="badge">not on disk</span>{/if}</span>
              </button>
            </li>
          {:else}
            <li class="muted small" style="padding:.4rem">{searching ? 'Searching…' : 'No match.'}</li>
          {/each}
          <li><button class="ghost item" onclick={() => (manual = { title: q, year: '', kind: 'show', episode_minutes: '', transient: true })}>+ Not in the list: add "{q}" by name</button></li>
        </ul>
      {/if}
    </div>
    {#if manual}
      <div class="card">
        <div class="card-title"><h3>Add by name</h3><button class="small ghost" onclick={() => (manual = null)} aria-label="Cancel">✕</button></div>
        <div class="form-grid">
          <label class="field">Title<input bind:value={manual.title} /></label>
          <label class="field">Year<input class="narrow" type="number" min="1900" max="2100" bind:value={manual.year} /></label>
          <label class="field">Kind<select bind:value={manual.kind}><option value="show">Series</option><option value="movie">Film</option></select></label>
          {#if manual.kind === 'show'}<label class="field">Episode minutes<input class="narrow" type="number" min="1" max="240" bind:value={manual.episode_minutes} placeholder="default" /><span class="help">Used for scheduling until the files arrive.</span></label>{/if}
          <label class="check"><input type="checkbox" bind:checked={manual.transient} /> Transient<span class="help">Fetched into the cache for its airing and removed afterwards rather than kept on the NAS.</span></label>
        </div>
        <div class="row mt"><button class="primary" onclick={addManual} disabled={busy || !manual.title.trim()}>Add to line-up</button></div>
      </div>
    {/if}

    <DataTable id="lineup-entries" {columns} rows={entries} card={false} search="Filter this line-up…"
      empty="Nothing in this line-up yet. Add titles above or run Generate on the Channels page." rowClass={(e) => (e.enabled ? '' : 'off')} />
    <p class="tiny muted">Pinned entries survive rebalance; a series or film can be on one channel only. Entries not on disk are scheduled ahead and fetched by pitv_content when the channel allows it.</p>
  </div>
  {#snippet footer()}
    <span class="small muted" style="flex:1">{entries?.length ?? 0} entries</span>
    <button onclick={onclose}>Close</button>
  {/snippet}
</Drawer>

{#snippet titleCell(e)}<b>{e.title}</b>{#if e.year}<span class="muted small">{` (${e.year})`}</span>{/if}{#if e.pinned}<span class="badge" title="Pinned: survives rebalance">📌</span>{/if}
  <div class="tiny muted">{e.source}{e.external ? ' · external' : ''}{e.next_episode ? ` · next ${e.next_episode}` : ''}{e.episode_minutes ? ` · ${e.episode_minutes} min` : ''}{e.wanted_done ? ` · ${e.wanted_done} fetched` : ''}</div>{/snippet}
{#snippet kindCell(e)}<span class="badge">{kindLabel(e)}</span>{/snippet}
{#snippet stateCell(e)}{@const [cls, txt] = lineupState(e)}<span class="badge {cls}">{txt}</span>{/snippet}
{#snippet flag(e, c)}<input type="checkbox" checked={!!e[c.key]} onchange={(ev) => setField(e, c.key, ev.currentTarget.checked)} aria-label={c.title} />{/snippet}
{#snippet moveCell(e)}<select onchange={(ev) => { move(e, ev.currentTarget.value); ev.currentTarget.value = ''; }} aria-label="Move to channel" style="min-height:28px;padding:.15rem .3rem"><option value="">…</option>{#each others as c (c.id)}<option value={c.id}>{c.number} {c.short_name}</option>{/each}</select>{/snippet}
{#snippet actionsCell(e)}<button class="small danger" onclick={() => remove(e)}>Remove</button>{/snippet}

<style>
  .add { position: relative; }
  .add input { width: 100%; }
  .opts { list-style: none; margin: .3rem 0 0; padding: .2rem; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--bg-elev); max-height: 280px; overflow: auto; display: flex; flex-direction: column; }
  .item { width: 100%; display: flex; justify-content: space-between; gap: .5rem; text-align: left; min-height: 0; padding: .35rem .5rem; }
  .item > span:first-child { flex: 1; min-width: 0; }
  /* A disabled entry is dimmed, but its On checkbox and Remove button must stay readable. */
</style>
