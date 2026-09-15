<script>
  let { providers = [] } = $props();
</script>

<div class="card pad-0 table-wrap">
  <table>
    <thead><tr><th>Provider</th><th>Enabled</th><th>Health</th><th>Detail</th></tr></thead>
    <tbody>
      {#each providers as p (p.id)}
        <tr>
          <td><b>{p.name ?? p.id}</b><div class="tiny muted mono">{p.id}</div></td>
          <td>{#if p.enabled}<span class="badge ok">enabled</span>{:else}<span class="badge">disabled</span>{/if}</td>
          <td>{#if p.healthy === true}<span class="badge ok">healthy</span>{:else if p.healthy === false}<span class="badge danger">unhealthy</span>{:else}<span class="badge">unknown</span>{/if}</td>
          <td class="small muted">{p.detail ?? ''}</td>
        </tr>
      {:else}
        <tr><td colspan="4" class="empty">No providers reported.</td></tr>
      {/each}
    </tbody>
  </table>
</div>
