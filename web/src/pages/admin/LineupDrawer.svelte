<script>
  // A channel's line-up: the series and films it carries, including material pitv_content still has to fetch.
  import AppBadge from '../../components/AppBadge.svelte';
  import { untrack } from 'svelte';
  import { get, post, put, del, tryApi } from '../../lib/api.js';
  import { confirm, toast } from '../../lib/stores.svelte.js';
  import Drawer from '../../components/Drawer.svelte';

  let { channel, channels = [], onclose, onchanged } = $props();
  let entries = $state(null);
  let q = $state('');
  let options = $state([]);
  let searching = $state(false);
  let manual = $state(null);   // {title, year, kind, episode_minutes, transient}
  let busy = $state(false);
  let timer;

  async function load() {
    entries = (await tryApi(get('/api/lineup', { channel_id: channel.id }))) ?? [];
    onchanged?.();
  }
  $effect(() => { channel.id; untrack(load); }); // eslint-disable-line no-unused-expressions
  async function search(v) {
    q = v; clearTimeout(timer);
    timer = setTimeout(async () => { searching = true; options = (await tryApi(get('/api/lineup/options', { q, limit: 30 }))) ?? []; searching = false; }, 250);
  }
  async function addOption(o) {
    const body = { channel_id: channel.id };
    if (o.show_id) body.show_id = o.show_id; else if (o.media_id) body.media_id = o.media_id;
    else Object.assign(body, { title: o.title, year: o.year ?? null, kind: 'show', catalogue: !!o.catalogue });
    busy = true;
    const r = await tryApi(post('/api/lineup', body), { success: `${o.title} added${o.channel_id && o.channel_id !== channel.id ? ` (moved from Ch ${o.channel_number})` : ''}` });
    busy = false;
    if (r) { q = ''; options = []; load(); }
  }
  async function addManual() {
    const body = { channel_id: channel.id, title: manual.title.trim(), year: manual.year === '' ? null : Number(manual.year), kind: manual.kind, transient: manual.transient,
                   episode_minutes: manual.kind === 'show' && manual.episode_minutes !== '' ? Number(manual.episode_minutes) : null };
    busy = true;
    const r = await tryApi(post('/api/lineup', body), { success: `${body.title} added; pitv_content will look for it` });
    busy = false;
    if (r) { manual = null; load(); }
  }
  async function setField(e, field, value) {
    const r = await tryApi(put(`/api/lineup/${e.id}`, { [field]: value }));
    if (r) load();
  }
  async function move(e, channelId) {
    const target = channels.find((c) => c.id === Number(channelId));
    if (!target || target.id === channel.id) return;
    if (await tryApi(put(`/api/lineup/${e.id}`, { channel_id: target.id }), { success: `${e.title} moved to ${target.name}` })) load();
  }
  async function remove(e) {
    if (!(await confirm(`Remove "${e.title}" from ${channel.name}? It will have no channel until the generator or you place it again.`, { title: 'Remove from line-up', okLabel: 'Remove', danger: true }))) return;
    if (await tryApi(del(`/api/lineup/${e.id}`))) load();
  }
  function stateOf(e) {
    if (e.wanted_open) return ['info', `fetching ${e.wanted_open}`];
    if (e.placeholders) return ['warn', `${e.placeholders} scheduled`];
    if (e.on_disk) return ['ok', e.kind === 'show' && e.episodes_on_disk ? `on disk (${e.episodes_on_disk} eps)` : 'on disk'];
    return ['', 'not on disk'];
  }
  let others = $derived(channels.filter((c) => c.id !== channel.id && c.enabled && ['general', 'cartoons'].includes(c.content ?? 'general')));
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
        <div class="card-title"><h3>Add by name</h3><button class="small ghost" onclick={() => (manual = null)}>✕</button></div>
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

    <div class="table-wrap">
      <table>
        <thead><tr><th>Title</th><th>Kind</th><th>State</th><th title="Enabled">On</th><th title="Transient: fetched for airing then removed">Trans.</th><th title="Remove after airing">Rm. after</th><th>Move to</th><th></th></tr></thead>
        <tbody>
          {#each entries ?? [] as e (e.id)}
            {@const [cls, txt] = stateOf(e)}
            <tr class:off={!e.enabled}>
              <td><b>{e.title}</b>{#if e.year}<span class="muted small"> ({e.year})</span>{/if}{#if e.pinned}<span class="badge" title="Pinned: survives rebalance">📌</span>{/if}
                <div class="tiny muted">{e.source}{e.external ? ' · external' : ''}{e.next_episode ? ` · next ${e.next_episode}` : ''}{e.episode_minutes ? ` · ${e.episode_minutes} min` : ''}{e.wanted_done ? ` · ${e.wanted_done} fetched` : ''}</div></td>
              <td><span class="badge">{e.kind === 'movie' ? 'film' : 'series'}</span></td>
              <td><span class="badge {cls}">{txt}</span></td>
              <td><input type="checkbox" checked={!!e.enabled} onchange={(ev) => setField(e, 'enabled', ev.currentTarget.checked)} aria-label="Enabled" /></td>
              <td><input type="checkbox" checked={!!e.transient} onchange={(ev) => setField(e, 'transient', ev.currentTarget.checked)} aria-label="Transient" /></td>
              <td><input type="checkbox" checked={!!e.remove_after_airing} onchange={(ev) => setField(e, 'remove_after_airing', ev.currentTarget.checked)} aria-label="Remove after airing" /></td>
              <td><select onchange={(ev) => { move(e, ev.currentTarget.value); ev.currentTarget.value = ''; }} aria-label="Move to channel" style="min-height:28px;padding:.15rem .3rem"><option value="">…</option>{#each others as c (c.id)}<option value={c.id}>{c.number} {c.short_name}</option>{/each}</select></td>
              <td class="right"><button class="small danger" onclick={() => remove(e)}>Remove</button></td>
            </tr>
          {:else}
            <tr><td colspan="8" class="empty">{entries ? 'Nothing in this line-up yet. Add titles above or run Generate on the Channels page.' : 'Loading…'}</td></tr>
          {/each}
        </tbody>
      </table>
    </div>
    <p class="tiny muted">Pinned entries survive rebalance; a series or film can be on one channel only. Entries not on disk are scheduled ahead and fetched by pitv_content when the channel allows it.</p>
  </div>
  {#snippet footer()}
    <span class="small muted" style="flex:1">{entries?.length ?? 0} entries</span>
    <button onclick={onclose}>Close</button>
  {/snippet}
</Drawer>

<style>
  .add { position: relative; }
  .add input { width: 100%; }
  .opts { list-style: none; margin: .3rem 0 0; padding: .2rem; border: 1px solid var(--border-strong); border-radius: var(--radius-sm); background: var(--bg-elev); max-height: 280px; overflow: auto; display: flex; flex-direction: column; }
  .item { width: 100%; display: flex; justify-content: space-between; gap: .5rem; text-align: left; min-height: 0; padding: .35rem .5rem; }
  .item > span:first-child { flex: 1; min-width: 0; }
  tr.off td { opacity: .55; }
  tr.off td:last-child, tr.off td:nth-child(4) { opacity: 1; }
</style>
