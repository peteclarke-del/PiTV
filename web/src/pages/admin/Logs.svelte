<script>
  import { untrack } from 'svelte';
  import AppBadge from '../../components/AppBadge.svelte';
  import { get, tryApi } from '../../lib/api.js';
  import { toast, clock } from '../../lib/stores.svelte.js';
  import { fmtBytes, fmtAgo } from '../../lib/format.js';
  import { debounce } from '../../lib/util.js';
  import { poll } from '../../lib/poll.svelte.js';

  /** fixed: show only this source and hide the selector (e.g. 'pitv-content' on the Content tab). */
  let { fixed = null, height = 'calc(100vh - 260px)', liveTool = false } = $props();
  // [value, label, owning app]. The catalogue log records imports of pitv_content's index, which is PiTV's work.
  const SOURCES = [
    ['player', 'Player log', 'pitv'], ['web', 'Web log', 'pitv'], ['catalogue', 'Catalogue import log', 'pitv'],
    ['schedule', 'Schedule log', 'pitv'], ['install', 'Install log', 'pitv'],
    ['journal:pitv-player', 'Journal: pitv-player', 'pitv'], ['journal:pitv-web', 'Journal: pitv-web', 'pitv'],
    ['pitv-content', 'pitv_content log', 'content'],
  ];
  const APP_LABEL = { pitv: 'PiTV', content: 'pitv_content' };
  const LEVELS = ['', 'DEBUG', 'INFO', 'WARNING', 'ERROR'];
  let source = $state(untrack(() => fixed) ?? 'player');
  let logPath = $state('');
  let lines = $state(300);
  let level = $state('');
  let q = $state('');
  let auto = $state(false);
  let files = $state([]);
  let entries = $state(null);     // file log: [{ts, level, logger, msg}]
  let journal = $state(null);     // {unit, text, error}
  let exists = $state(true);
  let loading = $state(false);
  let box = $state(null);
  let atEnd = $state(true);

  let isJournal = $derived(source.startsWith('journal:'));
  let fileInfo = $derived(files.find((f) => f.name === source));
  let journalLines = $derived.by(() => {
    if (!journal) return [];
    let out = journal.text.split('\n').filter((l) => l.trim());
    if (q) out = out.filter((l) => l.toLowerCase().includes(q.toLowerCase()));
    return out;
  });

  async function load() {
    loading = true;
    try {
      if (isJournal) {
        journal = await get(`/api/logs/journal/${encodeURIComponent(source.slice(8))}`, { lines });
        entries = null; exists = true;
      } else {
        let r = null;
        if (source === 'pitv-content') {
          // Prefer pitv_content's own API when it answers; otherwise read the log file through PiTV.
          if (liveTool) { try { r = await get('/api/content/tool/api/log', { lines, q, level }); r.exists = true; } catch { r = null; } }
          if (!r) r = await get('/api/content/tool/log', { lines, q, level });
        } else {
          r = await get(`/api/logs/${encodeURIComponent(source)}`, { lines, q, level });
        }
        entries = r.lines ?? []; exists = r.exists ?? true; journal = null; logPath = r.path ?? '';
      }
    } catch (e) {
      toast.error(e.detail || e.message);
    }
    loading = false;
    if (atEnd) requestAnimationFrame(jumpToEnd);
  }
  function jumpToEnd() { if (box) { box.scrollTop = box.scrollHeight; atEnd = true; } }
  function onScroll() { if (box) atEnd = box.scrollHeight - box.scrollTop - box.clientHeight < 40; }
  // The file list (path, size, mtime) is per source, not per tail: auto-refresh only re-reads the tail.
  async function loadFiles() { files = (await tryApi(get('/api/logs'))) ?? files; }
  $effect(() => { source; untrack(loadFiles); });
  $effect(() => { source; lines; level; liveTool; untrack(load); });
  const reload = debounce(load, 300);
  function onQuery(v) { q = v; reload(); }
  poll(load, 5000, () => auto);

  function asText() {
    if (isJournal) return journalLines.join('\n');
    return (entries ?? []).map((e) => `${e.ts} ${e.level.padEnd(7)} ${e.logger} ${e.msg}`).join('\n');
  }
  async function copy() {
    try { await navigator.clipboard.writeText(asText()); toast.success('Copied to clipboard'); }
    catch { toast.error('Clipboard unavailable; select the text and copy it manually'); }
  }
  const levelClass = (l) => (l === 'ERROR' || l === 'CRITICAL' ? 'err' : l === 'WARNING' ? 'warn' : l === 'DEBUG' ? 'dbg' : '');
  const journalClass = (l) => (/ error|traceback|critical/i.test(l) ? 'err' : /warning/i.test(l) ? 'warn' : '');
</script>

<div class="stack">
  <div class="row">
    {#if !fixed}
      <select bind:value={source} aria-label="Log">
        {#each ['pitv', 'content'] as app (app)}
          <optgroup label={APP_LABEL[app]}>{#each SOURCES.filter((x) => x[2] === app) as [v, l] (v)}<option value={v}>{APP_LABEL[app]}: {l}</option>{/each}</optgroup>
        {/each}
      </select>
      <AppBadge app={SOURCES.find((x) => x[0] === source)?.[2] ?? 'pitv'} />
    {/if}
    <select bind:value={lines} aria-label="Lines">{#each [100, 300, 1000, 3000] as n (n)}<option value={n}>{n} lines</option>{/each}</select>
    {#if !isJournal}
      <select bind:value={level} aria-label="Minimum level">{#each LEVELS as l (l)}<option value={l}>{l || 'All levels'}</option>{/each}</select>
    {/if}
    <input type="search" placeholder="Filter…" aria-label="Filter" value={q} oninput={(e) => onQuery(e.currentTarget.value)} style="width:200px" />
    <label class="check small"><input type="checkbox" bind:checked={auto} /> Auto-refresh (5 s)</label>
    <span class="spacer"></span>
    <button class="small" onclick={load} disabled={loading}>Refresh</button>
    <button class="small" onclick={copy}>Copy</button>
    <button class="small" onclick={jumpToEnd}>Jump to end ↓</button>
  </div>
  {#if fileInfo}
    <div class="tiny muted mono">{fileInfo.path} · {fmtBytes(fileInfo.size)}{fileInfo.modified ? ` · updated ${fmtAgo(fileInfo.modified, clock.ts)}` : ''}</div>
  {:else if source === 'pitv-content' && logPath}
    <div class="tiny muted mono">{logPath}</div>
  {/if}

  <div class="log" bind:this={box} onscroll={onScroll} class:loading style="height:{height}">
    {#if isJournal}
      {#if journal?.error}<div class="line err">{journal.error}</div>{/if}
      {#each journalLines as l, i (i)}<div class="line {journalClass(l)}">{l}</div>{:else}<div class="line muted">{journal ? 'Journal is empty (only available on the Pi under systemd).' : 'Loading…'}</div>{/each}
    {:else if entries}
      {#each entries as e, i (i)}
        <div class="line {levelClass(e.level)}"><span class="ts">{e.ts}</span> <span class="lv">{e.level}</span> <span class="lg">{e.logger}</span> <span class="msg">{e.msg}</span></div>
      {:else}
        <div class="line muted">{exists ? 'No matching lines.' : source === 'pitv-content' ? 'No pitv_content log yet: the tool writes it while it runs.' : 'This log file does not exist yet.'}</div>
      {/each}
    {:else}
      <div class="line muted">Loading…</div>
    {/if}
  </div>
  {#if !atEnd}<button class="small jump" onclick={jumpToEnd}>Newest ↓</button>{/if}
</div>

<style>
  .log { position: relative; font-family: var(--mono); font-size: .78rem; line-height: 1.4; background: var(--bg-sunken); border: 1px solid var(--border); border-radius: var(--radius); padding: .5rem .6rem; min-height: 200px; overflow: auto; }
  .log.loading { opacity: .7; }
  .line { white-space: pre-wrap; word-break: break-word; padding: .05rem 0; border-bottom: 1px dotted color-mix(in srgb, var(--border) 60%, transparent); }
  .ts { color: var(--fg-faint); }
  .lv { font-weight: 700; }
  .lg { color: var(--info); }
  .warn { color: #9a6300; background: color-mix(in srgb, var(--warn) 14%, transparent); }
  .err { color: var(--danger); background: color-mix(in srgb, var(--danger) 10%, transparent); }
  .dbg { color: var(--fg-muted); }
  @media (prefers-color-scheme: dark) { .warn { color: var(--warn); } }
  .jump { position: sticky; bottom: 12px; margin-left: auto; box-shadow: var(--shadow); }
</style>
