<script>
  import { untrack } from 'svelte';
  import { get, post, put, del, tryApi } from '../../lib/api.js';
  import { changes, confirm, clock } from '../../lib/stores.svelte.js';
  import { fmtAgo } from '../../lib/format.js';
  import Drawer from '../../components/Drawer.svelte';
  import FolderPicker from './FolderPicker.svelte';
  import JobList from './JobList.svelte';

  const TYPES = [['tv', 'TV shows'], ['movie', 'Movies'], ['advert', 'Adverts'], ['ident', 'Idents'], ['music', 'Music videos']];
  let sources = $state(null);
  let editing = $state(null); // {id?, type, name, path, remote, enabled}
  let picking = $state(false);
  let saving = $state(false);

  async function load() {
    try { sources = await get('/api/sources'); } catch { sources = sources ?? []; }
  }
  $effect(() => { changes.library; untrack(load); }); // eslint-disable-line no-unused-expressions

  const CATEGORIES = [['general', 'General'], ['sport', 'Sport'], ['kids', 'Children\'s']];
  function add() { editing = { type: 'tv', name: '', path: '', remote: '', enabled: true, category: 'general' }; }
  function edit(s) { editing = { ...s, enabled: !!s.enabled, category: s.category || 'general' }; }
  async function save() {
    saving = true;
    const body = { type: editing.type, name: editing.name, path: editing.path, remote: editing.remote || null, enabled: editing.enabled,
                   category: editing.type === 'tv' ? editing.category || 'general' : 'general' };
    const r = await tryApi(editing.id ? put(`/api/sources/${editing.id}`, body) : post('/api/sources', body), { success: 'Source saved' });
    saving = false;
    if (r) { editing = null; load(); }
  }
  async function remove(s) {
    if (!(await confirm(`Delete source "${s.name}"? Its ${s.item_count} scanned items will be removed from the library.`, { title: 'Delete source', okLabel: 'Delete', danger: true }))) return;
    if (await tryApi(del(`/api/sources/${s.id}`), { success: 'Source deleted' })) load();
  }
  const scan = (s) => tryApi(post(`/api/sources/${s.id}/scan`), { success: `Scanning ${s.name}` });
  const scanAll = () => tryApi(post('/api/scan'), { success: 'Scan started' });
</script>

<div class="stack">
  <div class="row">
    <button class="primary" onclick={add}>Add source</button>
    <button onclick={scanAll} disabled={!sources?.length}>Scan all</button>
  </div>

  <div class="card pad-0 table-wrap">
    <table>
      <thead><tr><th>Name</th><th>Type</th><th>Path</th><th>Status</th><th class="num">Items</th><th>Last scan</th><th></th></tr></thead>
      <tbody>
        {#each sources ?? [] as s (s.id)}
          <tr>
            <td><b>{s.name}</b>{#if !s.enabled}<span class="badge">disabled</span>{/if}</td>
            <td>{TYPES.find((t) => t[0] === s.type)?.[1] ?? s.type}{#if s.category && s.category !== 'general'}<span class="badge info">{s.category}</span>{/if}</td>
            <td class="mono small" style="max-width:280px" title={s.path}><div class="truncate">{s.path}</div>{#if s.remote}<div class="tiny muted truncate">{s.remote}</div>{/if}</td>
            <td>{#if s.available}<span class="badge ok">available</span>{:else}<span class="badge danger">missing</span>{/if}</td>
            <td class="num">{s.item_count}</td>
            <td class="small"><div>{fmtAgo(s.last_scanned_at, clock.ts)}</div>{#if s.last_scan_summary}<div class="tiny muted">{s.last_scan_summary}</div>{/if}</td>
            <td class="nowrap right">
              <button class="small" onclick={() => scan(s)} disabled={!s.available}>Scan</button>
              <button class="small" onclick={() => edit(s)}>Edit</button>
              <button class="small danger" onclick={() => remove(s)}>Delete</button>
            </td>
          </tr>
        {:else}
          <tr><td colspan="7" class="empty">{sources ? 'No sources yet. Add the folders where your TV shows, movies, adverts and idents live.' : 'Loading…'}</td></tr>
        {/each}
      </tbody>
    </table>
  </div>

  <div class="card"><div class="card-title"><h3>Scan jobs</h3></div><JobList kind="scan" /></div>
</div>

<Drawer open={!!editing} title={editing?.id ? 'Edit source' : 'Add source'} onclose={() => (editing = null)}>
  {#if editing}
    <div class="stack">
      <label class="field">Type
        <select bind:value={editing.type}>{#each TYPES as [v, l] (v)}<option value={v}>{l}</option>{/each}</select>
        <span class="help">{editing.type === 'music' ? 'Music videos: Genre/Artist - Title (1984).mp4; concerts under a Concerts/ folder or anything over 35 minutes.' : 'TV sources are scanned as Show/Season/Episode folders; movies, adverts and idents as flat files.'}</span>
      </label>
      <label class="field">Name<input bind:value={editing.name} placeholder="e.g. NAS TV shows" /></label>
      {#if editing.type === 'tv'}
        <label class="field">Category
          <select bind:value={editing.category}>{#each CATEGORIES as [v, l] (v)}<option value={v}>{l}</option>{/each}</select>
          <span class="help">Sport sources get weekend afternoon and midweek late slots and may run back to back at weekends.</span>
        </label>
      {/if}
      <label class="field">Local path
        <div class="row"><input bind:value={editing.path} placeholder="/mnt/nas/tvshows" style="flex:1" /><button type="button" onclick={() => (picking = true)}>Browse…</button></div>
        <span class="help">Folder as mounted on the Pi.</span>
      </label>
      <label class="field">Remote (optional)<input bind:value={editing.remote} placeholder="smb://synologynas/tvshows/" /><span class="help">Informational only.</span></label>
      <label class="check"><input type="checkbox" bind:checked={editing.enabled} /> Enabled</label>
    </div>
  {/if}
  {#snippet footer()}
    <button onclick={() => (editing = null)}>Cancel</button>
    <button class="primary" onclick={save} disabled={saving || !editing?.path}>Save</button>
  {/snippet}
</Drawer>

<FolderPicker open={picking} start={editing?.path || '/'} onclose={() => (picking = false)} onpick={(p) => { editing.path = p; if (!editing.name) editing.name = p.split('/').filter(Boolean).at(-1) ?? ''; picking = false; }} />
