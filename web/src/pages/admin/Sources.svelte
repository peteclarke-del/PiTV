<script>
  // pitv_content's sources: the NAS shares it indexes and the cache folders holding what it fetched.
  // PiTV no longer scans anything; this page reads the list through PiTV and relays edits to pitv_content.
  import { untrack } from 'svelte';
  import { get, post, put, tryApi, confirmApi } from '../../lib/api.js';
  import { importCatalogue } from '../../lib/actions.js';
  import { changes, clock, toast } from '../../lib/stores.svelte.js';
  import { guard } from '../../lib/guard.svelte.js';
  import { fmtAgo } from '../../lib/format.js';
  import { shown } from '../../lib/prefs.svelte.js';
  import AppBadge from '../../components/AppBadge.svelte';
  import DataTable from '../../components/DataTable.svelte';
  import Drawer from '../../components/Drawer.svelte';
  import FolderPicker from './FolderPicker.svelte';

  const TYPES = [['tv', 'TV shows'], ['movie', 'Movies'], ['advert', 'Adverts'], ['ident', 'Idents'], ['music', 'Music videos']];
  const CATEGORIES = [['general', 'General'], ['sport', 'Sport'], ['kids', 'Children\'s']];
  const LOCATIONS = [['nas', 'NAS shares', 'Indexed read-only; pitv_content copies or transcodes from here into the cache before a programme airs.',
                      'No NAS shares. Add the folders where TV shows, films, adverts, idents and music videos live.'],
                     ['cache', 'Cache folders', 'Material pitv_content fetched online, filed under acquire_dir; it already plays from the cache.', 'Nothing fetched yet.']];
  let doc = $state(null);          // {owner, offline, error, sources}
  let editing = $state(null);      // form, with isNew
  let errors = $state({});
  let picking = $state(false);

  async function load() {
    doc = (await tryApi(get('/api/sources'))) ?? doc ?? { offline: true, sources: [] };
  }
  $effect(() => { changes.library; untrack(load); });

  let offline = $derived(!!doc?.offline);
  let byLocation = $derived(LOCATIONS.map(([loc, label, help, empty]) =>
    [loc, label, help, empty, doc ? (doc.sources ?? []).filter((s) => (s.location ?? 'nas') === loc) : null]));
  const typeLabel = (t) => TYPES.find((x) => x[0] === t)?.[1] ?? t;
  const indexedAt = (h) => h?.last_indexed_ts ?? h?.last_indexed ?? null;
  // Worst first, so an ascending sort on Health lists the problems at the top.
  // Worst first, so sorting the column ascending puts problems at the top.
  const HEALTH = [['danger', 'mount failed'], ['danger', 'not mounted'], ['danger', 'unreadable'], ['info', 'mounting'],
                  ['', 'unknown', "Health is reported by pitv_content's API"], ['ok', 'mounted']];
  function healthRank(h) {
    if (h?.mount_state === 'failed') return 0;
    if (h?.mounted === false && h?.mount_state !== 'pending') return 1;
    if (h?.readable === false) return 2;
    if (h?.mount_state === 'pending') return 3;
    return h?.mounted === true ? 5 : 4;
  }
  const columns = [
    { key: 'name', label: 'Name', cell: nameCell },
    { key: 'type', label: 'Type', get: (s) => typeLabel(s.type), cell: typeCell },
    { key: 'root', label: 'Root', class: 'mono small', cell: rootCell },
    { key: 'health', label: 'Health', get: (s) => healthRank(s.health), cell: healthCell },
    { key: 'items', label: 'Items', class: 'num', get: (s) => s.health?.items },
    { key: 'indexed', label: 'Last indexed', class: 'small muted nowrap', get: (s) => indexedAt(s.health), cell: indexedCell },
    { key: 'actions', label: '', class: 'nowrap right', sortable: false, cell: actionsCell },
  ];

  // Credentials: the password is write-only. pitv_content says whether one is saved, never what it is;
  // a blank password keeps the saved one.
  const noCredentials = { username: '', password: '', workgroup: '', saved: false, clear: false };
  function add() { errors = {}; tested = null; editing = { isNew: true, id: '', name: '', type: 'tv', category: 'general', root: '', mount: '', remote: '', enabled: true, ...noCredentials }; }
  function edit(s) {
    errors = {}; tested = null;
    const c = s.credentials ?? {};
    editing = { isNew: false, id: s.id, name: s.name ?? '', type: s.type, category: s.category || 'general', root: s.root ?? '', mount: s.mount ?? '', remote: s.remote ?? '',
                enabled: s.enabled !== false, ...noCredentials, username: c.username ?? '', workgroup: c.workgroup ?? '', saved: !!c.set };
  }
  let smb = $derived(/^smb:\/\//i.test(editing?.remote?.trim() ?? ''));
  /** The credential fields to send: none unless the share is SMB and a login is entered or saved
   *  (a pitv_content without login support refuses fields it does not know), a clear when asked. */
  function credentials(f) {
    if (!smb) return {};
    if (f.clear) return { clear_credentials: true };
    const out = {};
    if (f.username.trim() || f.saved) out.username = f.username.trim() || null;
    if (f.workgroup.trim() || f.saved) out.workgroup = f.workgroup.trim() || null;
    if (f.password) out.password = f.password;
    return out;
  }
  let tested = $state(null);   // {ok, message} from the last connection test
  // A result describes the address and login it was run with; editing either retires it.
  $effect(() => { editing?.remote; editing?.username; editing?.password; editing?.workgroup; editing?.clear; tested = null; });
  const test = guard(async () => {
    const f = editing;
    tested = null;
    try {
      const r = await post('/api/sources/test', { id: f.isNew ? null : f.id, remote: f.remote.trim(), root: f.root.trim(), ...credentials(f) });
      tested = { ok: !!r?.ok, message: r?.message ?? (r?.ok ? 'The share answered.' : 'The share did not answer.') };
    } catch (e) {
      tested = { ok: false, message: e?.status === 404 ? 'This version of pitv_content cannot test a share yet.' : explain(e) };
    }
  });

  // PiTV relays to pitv_content: 400 carries per-field errors, 403 a root outside the allowed roots, 503 an offline tool.
  function explain(e) {
    const body = e?.body ?? {};
    // PiTV relays pitv_content's {errors: {...}} as {detail: {...}}; its own checks send a plain string.
    const fe = body.errors ?? body.detail?.errors ?? (body.detail && typeof body.detail === 'object' ? body.detail : null);
    if (e?.status === 400 && fe && typeof fe === 'object') { errors = fe; return 'pitv_content rejected some fields'; }
    if (e?.status === 403) { errors = { root: e.detail || 'outside the folders pitv_content may index' }; return 'That root is outside the allowed folders'; }
    if (e?.status === 503) { load(); return 'pitv_content is not running; sources can only be changed through it'; }
    return e?.detail || e?.message || String(e);
  }
  const save = guard(async () => {
    errors = {};
    const f = editing;
    const body = { id: f.id.trim(), name: f.name.trim(), type: f.type, root: f.root.trim(), mount: f.mount.trim(),
                   remote: f.remote.trim() || null, enabled: f.enabled, ...credentials(f) };
    if (f.type === 'tv') body.category = f.category || 'general';
    try {
      await put('/api/sources', body);
      toast.success(`Source ${body.name || body.id} saved; re-index to pick up its files`);
      editing = null;
      load();
    } catch (e) { toast.error(explain(e)); }
  });
  async function remove(s) {
    // Rethrow with the explained message so tryApi toasts it instead of the success line.
    const r = await confirmApi(`Remove source "${s.name}" from pitv_content? Its items leave PiTV's catalogue at the next complete import; line-up entries for them stay, as external material.`,
      { title: 'Remove source', okLabel: 'Remove', danger: true }, async () => {
        try { return await put('/api/sources', { id: s.id, delete: true }); } catch (e) { throw new Error(explain(e)); }
      }, { success: `Source ${s.name} removed` });
    if (r) load();
  }
  const reindex = guard(() => importCatalogue(true));
</script>

<div class="stack">
  <div class="card">
    <div class="card-title"><h3>Sources</h3><AppBadge app="content" />
      <span class="spacer"></span>
      <button class="small" onclick={reindex} disabled={reindex.busy || offline} title="Ask pitv_content to re-index its sources, then import the result into PiTV's catalogue">Re-index sources and import</button>
      <button class="small primary" onclick={add} disabled={offline}>Add source</button>
    </div>
    <p class="scope">Owned by pitv_content: the folders it indexes and serves from. PiTV never reads these for scheduling; it imports pitv_content's library index instead (Catalogue).</p>
    {#if offline}
      <div class="warn-box">pitv_content is not running; showing the sources from the last index. Editing needs its API.{#if doc?.error}<div class="tiny muted mt">{doc.error}</div>{/if}</div>
    {/if}
  </div>

  {#each byLocation as [loc, label, help, empty, rows] (loc)}
    <div class="card pad-0">
      <div class="head"><h3>{label}</h3><span class="small muted">{help}</span></div>
      <DataTable id="sources-{loc}" {columns} {rows} card={false} {empty} rowClass={(s) => (s.enabled === false ? 'off' : '')} />
    </div>
  {/each}
</div>

{#snippet nameCell(s)}<b>{s.name}</b>{#if s.enabled === false}<span class="badge">disabled</span>{/if}<div class="tiny muted mono">{s.id}</div>{/snippet}
{#snippet typeCell(s)}{typeLabel(s.type)}{#if s.type === 'tv' && s.category && s.category !== 'general'}<span class="badge info">{s.category}</span>{/if}{/snippet}
{#snippet rootCell(s)}<div style="max-width:300px" title={s.root}><div class="truncate">{s.root}</div>{#if s.remote}<div class="tiny muted truncate">{s.remote}</div>{/if}</div>{/snippet}
{#snippet healthCell(s)}{@const [cls, text, title] = HEALTH[healthRank(s.health)]}<span class="badge {cls}" {title}>{text}</span>{#each [s.health?.mount_error, s.health?.error].filter(Boolean) as msg (msg)}<div class="tiny" style="color:var(--danger)">{msg}</div>{/each}{#if s.credentials?.set}<div class="tiny muted">login saved</div>{/if}{/snippet}
{#snippet indexedCell(s)}{indexedAt(s.health) ? fmtAgo(indexedAt(s.health), clock.ts) : 'never'}{/snippet}
{#snippet actionsCell(s)}<button class="small" onclick={() => edit(s)} disabled={offline}>Edit</button>
  <button class="small danger" onclick={() => remove(s)} disabled={offline}>Remove</button>{/snippet}

<Drawer open={!!editing} title={editing?.isNew ? 'Add source' : `Edit source ${editing?.id ?? ''}`} onclose={() => (editing = null)}>
  {#if editing}
    <div class="stack">
      <p class="scope" style="margin:0"><AppBadge app="content" /> Stored by pitv_content; PiTV relays the change. Re-index afterwards to pick up the files.</p>
      <label class="field">Id<input class="mono" bind:value={editing.id} disabled={!editing.isNew} placeholder="e.g. tvshows" /><span class="help">Stable key used in item uids; it cannot change once created.</span>{#if errors.id}<span class="help err">{errors.id}</span>{/if}</label>
      <label class="field">Name<input bind:value={editing.name} placeholder="e.g. TV Shows" />{#if errors.name}<span class="help err">{errors.name}</span>{/if}</label>
      <label class="field">Type
        <select bind:value={editing.type}>{#each TYPES as [v, l] (v)}<option value={v}>{l}</option>{/each}</select>
        <span class="help">{editing.type === 'music' ? 'Music videos: Genre/Artist - Title (1984).mp4; concerts under a Concerts/ folder or anything over 35 minutes.' : editing.type === 'tv' ? 'Show/Season/Episode folders in the Kodi layout.' : 'Flat files, one per title.'}</span>
        {#if errors.type}<span class="help err">{errors.type}</span>{/if}
      </label>
      {#if editing.type === 'tv'}
        <label class="field">Category
          <select bind:value={editing.category}>{#each CATEGORIES as [v, l] (v)}<option value={v}>{l}</option>{/each}</select>
          <span class="help">Sport series get weekend afternoon and midweek late slots; kids marks children's series.</span>
          {#if errors.category}<span class="help err">{errors.category}</span>{/if}
        </label>
      {/if}
      <label class="field">Root
        <div class="row"><input class="mono" bind:value={editing.root} placeholder="/mnt/tvshows" style="flex:1" /><button type="button" onclick={() => (picking = true)}>Browse…</button></div>
        <span class="help">Folder as mounted on the Pi. pitv_content only indexes inside its allowed roots.</span>
        {#if errors.root}<span class="help err">{errors.root}</span>{/if}
      </label>
      <label class="field">Mounted here (optional)
        <input class="mono" bind:value={editing.mount} placeholder="same as the root" />
        <span class="help">Where this machine finds that folder, if it is somewhere else. Needed when PiTV and pitv_content do not share their mount points; PiTV plays from here.</span>
        {#if errors.mount}<span class="help err">{errors.mount}</span>{/if}
      </label>
      <label class="field">Remote (optional)<input bind:value={editing.remote} placeholder="smb://synologynas/tvshows/" /><span class="help">The SMB share pitv_content mounts, read-only, at the root above.</span>{#if errors.remote}<span class="help err">{errors.remote}</span>{/if}</label>
      {#if smb}
        <fieldset class="creds">
          <legend>Share login {#if editing.saved}<span class="badge ok">saved</span>{/if}</legend>
          {#if !editing.clear}
            <div class="form-grid">
              <label class="field">Username<input bind:value={editing.username} autocomplete="off" placeholder="guest if empty" />{#if errors.username}<span class="help err">{errors.username}</span>{/if}</label>
              <label class="field">Password<input type="password" bind:value={editing.password} autocomplete="new-password" placeholder={editing.saved ? 'saved: leave blank to keep' : ''} />{#if errors.password}<span class="help err">{errors.password}</span>{/if}</label>
              {#if shown('advanced')}<label class="field">Workgroup or domain<input bind:value={editing.workgroup} autocomplete="off" placeholder="usually empty" /></label>{/if}
            </div>
          {/if}
          {#if editing.saved}<label class="check"><input type="checkbox" bind:checked={editing.clear} /> Remove the saved login<span class="help">The share is then mounted as a guest.</span></label>{/if}
          <p class="help">Kept by pitv_content where only its service can read it, and used only to mount this share. The password is never shown again.</p>
          <div class="row"><button type="button" class="small" onclick={test} disabled={test.busy || !editing.remote.trim()}>{test.busy ? 'Testing…' : 'Test connection'}</button>
            {#if tested}<span class="badge {tested.ok ? 'ok' : 'danger'}">{tested.ok ? 'connected' : 'failed'}</span><span class="small">{tested.message}</span>{/if}</div>
        </fieldset>
      {/if}
      <label class="check"><input type="checkbox" bind:checked={editing.enabled} /> Enabled<span class="help">Disabled sources are skipped by the next index.</span></label>
    </div>
  {/if}
  {#snippet footer()}
    <button onclick={() => (editing = null)}>Cancel</button>
    <button class="primary" onclick={save} disabled={save.busy || !editing?.id?.trim() || !editing?.root?.trim()}>Save</button>
  {/snippet}
</Drawer>

<FolderPicker open={picking} start={editing?.root || '/'} onclose={() => (picking = false)} onpick={(p) => { editing.root = p; if (!editing.name) editing.name = p.split('/').filter(Boolean).at(-1) ?? ''; if (editing.isNew && !editing.id) editing.id = (p.split('/').filter(Boolean).at(-1) ?? '').toLowerCase().replace(/[^a-z0-9]+/g, ''); picking = false; }} />

<style>
  .head { display: flex; flex-wrap: wrap; align-items: baseline; gap: .3rem 1rem; padding: .8rem 1rem .4rem; }
  .creds { border: 1px solid var(--border); border-radius: var(--radius-sm); padding: .6rem .8rem; display: flex; flex-direction: column; gap: .5rem; }
  .creds legend { font-weight: 650; font-size: .85rem; padding: 0 .3rem; }
  .head h3 { margin: 0; }
</style>
