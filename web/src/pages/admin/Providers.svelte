<script>
  // pitv_content's online providers, edited through its API (PiTV only relays the requests).
  import { onMount } from 'svelte';
  import { get, tryApi } from '../../lib/api.js';
  import { toolGet, isOffline } from '../../lib/toolapi.js';
  import { toast } from '../../lib/stores.svelte.js';
  import AppBadge from '../../components/AppBadge.svelte';
  import ToolProviders from './ToolProviders.svelte';

  let providers = $state(null);
  let online = $state(null);
  let toolUrl = $state('');

  async function load() {
    try {
      const list = await toolGet('providers');
      providers = Array.isArray(list) ? list : [];
      online = true;
    } catch (e) {
      online = false;
      if (!isOffline(e)) toast.error(e.detail || e.message);
    }
  }
  onMount(async () => {
    toolUrl = (await tryApi(get('/api/settings')))?.content_tool_url ?? '';
    load();
  });
</script>

<div class="stack">
  <div class="card">
    <div class="card-title"><h3>Online providers</h3><AppBadge app="content" /></div>
    <p class="scope">Where pitv_content looks for material that is not on the NAS. Changes are stored by pitv_content and apply to its next run; PiTV only relays them.</p>
    {#if online === false}
      <div class="warn-box">pitv_content API not reachable{toolUrl ? ` at ${toolUrl}` : ''}; is <code>pitv-content-api.service</code> running? Providers can only be read and changed through it.</div>
    {:else if online === null}
      <div class="skeleton" style="height:100px"></div>
    {/if}
  </div>
  {#if online}
    <ToolProviders {providers} onchange={(list) => { if (Array.isArray(list)) providers = list; else load(); }} />
  {/if}
</div>
