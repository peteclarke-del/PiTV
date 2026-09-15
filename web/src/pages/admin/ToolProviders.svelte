<script>
  // pitv_content sources: enable, order, kinds and options are edited in place; PUT providers sends one change at a time.
  import { toolPut } from '../../lib/toolapi.js';
  import { fieldErrors } from '../../lib/toolapi.js';
  import { toast } from '../../lib/stores.svelte.js';

  let { providers = [], onchange } = $props();
  let errors = $state({});
  let busy = $state('');
  let adding = $state(null);   // {type, options: [{k, v}]}
  let optionRows = $state({}); // id -> [{k, v}]
  let seeded = $state(null);
  $effect(() => {
    if (providers !== seeded) {
      seeded = providers;
      const m = {};
      for (const p of providers) m[p.id] = Object.entries(p.options ?? {}).map(([k, v]) => ({ k, v: String(v) }));
      optionRows = m;
    }
  });
  let types = $derived(providers[0]?.types ?? []);
  let kindsAvailable = $derived(providers[0]?.kinds_available ?? []);

  function validOptions(rows) {
    const errs = {}; const out = {};
    for (const r of rows) {
      const k = r.k.trim(); if (!k) continue;
      if (/url$/i.test(k) && r.v && !/^https:\/\//i.test(r.v)) errs[k] = 'must start with https://';
      out[k] = r.v !== '' && !Number.isNaN(Number(r.v)) && /^-?\d+(\.\d+)?$/.test(r.v) ? Number(r.v) : r.v;
    }
    return [out, errs];
  }
  async function send(body, label) {
    busy = body.id ?? 'new'; errors = {};
    try {
      const r = await toolPut('providers', body);
      toast.success(label);
      onchange?.(r?.providers);
    } catch (e) {
      const fe = fieldErrors(e);
      if (fe) { errors = fe; toast.error('pitv_content rejected the change'); } else toast.error(e.detail || e.message);
    }
    busy = '';
  }
  function saveOptions(p) {
    const [opts, errs] = validOptions(optionRows[p.id] ?? []);
    if (Object.keys(errs).length) { errors = errs; toast.error(Object.values(errs)[0]); return; }
    send({ id: p.id, options: opts }, `${p.name ?? p.id} options saved`);
  }
  function toggleKind(p, kind, on) {
    const kinds = new Set(p.kinds ?? []); if (on) kinds.add(kind); else kinds.delete(kind);
    send({ id: p.id, kinds: [...kinds] }, `${p.name ?? p.id} kinds updated`);
  }
  function addSource() {
    const [opts, errs] = validOptions(adding.options);
    if (Object.keys(errs).length) { errors = errs; toast.error(Object.values(errs)[0]); return; }
    send({ id: adding.id.trim(), type: adding.type, options: opts }, `Source ${adding.id} added`).then(() => { if (!Object.keys(errors).length) adding = null; });
  }
</script>

<div class="stack">
  <div class="row"><span class="small muted">Sources are tried in order; kinds limit what each may fetch. Options are passed to the provider as given.</span><span class="spacer"></span><button class="small primary" onclick={() => (adding = { id: '', type: types[0] ?? '', options: [{ k: '', v: '' }] })} disabled={!types.length}>Add source</button></div>
  {#if adding}
    <div class="card">
      <div class="card-title"><h3>New source</h3><button class="small ghost" onclick={() => (adding = null)}>✕</button></div>
      <div class="inline-form">
        <label class="field">Id<input class="mono" bind:value={adding.id} placeholder="e.g. archive2" /></label>
        <label class="field">Type<select bind:value={adding.type}>{#each types as t (t)}<option value={t}>{t}</option>{/each}</select></label>
      </div>
      <div class="opts mt">
        {#each adding.options as o, i (i)}
          <div class="row"><input class="mono" placeholder="option" bind:value={o.k} /><input placeholder="value" bind:value={o.v} /><button class="small ghost" onclick={() => adding.options.splice(i, 1)} aria-label="Remove option">✕</button></div>
        {/each}
        <div><button class="small ghost" onclick={() => adding.options.push({ k: '', v: '' })}>+ option</button></div>
      </div>
      {#if errors.id}<div class="help err">{errors.id}</div>{/if}{#if errors.type}<div class="help err">{errors.type}</div>{/if}
      <div class="row mt"><button class="primary" onclick={addSource} disabled={busy === 'new' || !adding.id.trim() || !adding.type}>Add</button></div>
    </div>
  {/if}
  <div class="card pad-0 table-wrap">
    <table>
      <thead><tr><th>Source</th><th>On</th><th>Order</th><th>Kinds</th><th>Health</th><th>Options</th></tr></thead>
      <tbody>
        {#each providers as p (p.id)}
          <tr class:off={!p.enabled}>
            <td><b>{p.name ?? p.id}</b><div class="tiny muted mono">{p.id} · {p.type}</div>{#if p.detail}<div class="tiny muted">{p.detail}</div>{/if}</td>
            <td><input type="checkbox" checked={!!p.enabled} disabled={busy === p.id} onchange={(e) => send({ id: p.id, enabled: e.currentTarget.checked }, `${p.name ?? p.id} ${e.currentTarget.checked ? 'enabled' : 'disabled'}`)} aria-label="Enabled" /></td>
            <td><input class="xnarrow" type="number" min="0" value={p.order ?? 0} onchange={(e) => send({ id: p.id, order: Number(e.currentTarget.value) }, `${p.name ?? p.id} order set`)} aria-label="Order" />{#if errors.order}<div class="help err">{errors.order}</div>{/if}</td>
            <td><div class="kinds">{#each kindsAvailable as k (k)}<label class="check small"><input type="checkbox" checked={(p.kinds ?? []).includes(k)} disabled={busy === p.id} onchange={(e) => toggleKind(p, k, e.currentTarget.checked)} />{k}</label>{/each}</div>{#if errors.kinds}<div class="help err">{errors.kinds}</div>{/if}</td>
            <td>{#if p.healthy === true}<span class="badge ok">healthy</span>{:else if p.healthy === false}<span class="badge danger">unhealthy</span>{:else}<span class="badge">unknown</span>{/if}</td>
            <td>
              <div class="opts">
                {#each optionRows[p.id] ?? [] as o, i (i)}
                  <div class="row"><input class="mono" placeholder="option" bind:value={o.k} /><input placeholder="value" bind:value={o.v} class:bad={errors[o.k]} /><button class="small ghost" onclick={() => optionRows[p.id].splice(i, 1)} aria-label="Remove option">✕</button></div>
                  {#if errors[o.k]}<div class="help err">{errors[o.k]}</div>{/if}
                {/each}
                <div class="row"><button class="small ghost" onclick={() => (optionRows[p.id] = [...(optionRows[p.id] ?? []), { k: '', v: '' }])}>+ option</button><button class="small" onclick={() => saveOptions(p)} disabled={busy === p.id}>Save options</button></div>
                {#if errors.options}<div class="help err">{errors.options}</div>{/if}
              </div>
            </td>
          </tr>
        {:else}
          <tr><td colspan="6" class="empty">No sources reported.</td></tr>
        {/each}
      </tbody>
    </table>
  </div>
</div>

<style>
  tr.off td { opacity: .6; }
  tr.off td:nth-child(2) { opacity: 1; }
  .kinds { display: flex; flex-wrap: wrap; gap: .2rem .6rem; }
  .opts { display: flex; flex-direction: column; gap: .25rem; min-width: 260px; }
  .opts .row { flex-wrap: nowrap; gap: .25rem; }
  .opts input { min-height: 28px; padding: .15rem .4rem; min-width: 0; }
  .opts input.mono { width: 40%; }
  .bad { border-color: var(--danger); }
  .help.err { color: var(--danger); font-size: .78rem; }
</style>
