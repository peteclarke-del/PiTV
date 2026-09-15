<script>
  // Readiness report notes grouped by their prefix. A bad morning can produce thousands of lines, so each
  // group is collapsed to a count and opens to its first entries.
  let { notes = [], limit = 40 } = $props();
  const KINDS = [
    ['NOT PLAYABLE', 'danger', 'No cached copy and no usable fallback: these airings get the technical difficulties card unless replaced.'],
    ['NOT FETCHED', 'danger', 'pitv_content has not delivered material that was never on the NAS.'],
    ['NOT CACHED', 'warn', 'Not in the cache yet; these play from the NAS while nas_fallback is on.'],
    ['REBUILT', 'info', 'The schedule was rebuilt around a missing item.'],
  ];
  let groups = $derived.by(() => {
    const out = KINDS.map(([prefix, cls, help]) => ({ prefix, cls, help, lines: [] }));
    const other = { prefix: 'Other', cls: '', help: '', lines: [] };
    for (const n of notes ?? []) {
      const g = out.find((x) => String(n).startsWith(x.prefix));
      (g ?? other).lines.push(g ? String(n).slice(g.prefix.length).trim() : String(n));
    }
    return [...out, other].filter((g) => g.lines.length);
  });
</script>

{#if groups.length}
  <div class="rn">
    {#each groups as g (g.prefix)}
      <details class="grp {g.cls}">
        <summary><span class="badge {g.cls}">{g.prefix}</span> <b>{g.lines.length}</b> <span class="tiny muted">{g.help}</span></summary>
        <pre class="log">{g.lines.slice(0, limit).join('\n')}{g.lines.length > limit ? `\n… and ${g.lines.length - limit} more` : ''}</pre>
      </details>
    {/each}
  </div>
{/if}

<style>
  .rn { display: flex; flex-direction: column; gap: .35rem; }
  .grp summary { cursor: pointer; display: flex; align-items: baseline; gap: .45rem; flex-wrap: wrap; padding: .2rem 0; }
  .grp pre { margin-top: .3rem; max-height: 220px; }
  .grp.danger pre { border-left: 3px solid var(--danger); }
  .grp.warn pre { border-left: 3px solid var(--warn); }
  .grp.info pre { border-left: 3px solid var(--info); }
</style>
