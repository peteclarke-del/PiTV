<script>
  // Where a catalogue item will play from: the cache (ready), the NAS (pitv_content copies it before air) or fetched online.
  let { item } = $props();
</script>

{#if item?.cached}
  <span class="badge ok" title={item.cache_path ? `In the cache: ${item.cache_path}` : 'In the cache; plays from local disk'}>cached</span>
{:else if item?.origin === 'online'}
  <span class="badge warn" title="Fetched online by pitv_content but no longer in the cache; it is requested again when scheduled">online</span>
{:else if item?.origin === 'cache'}
  <span class="badge warn" title="Indexed from the cache folder, but the file is not there now">cache (missing)</span>
{:else}
  <span class="badge" title="On the NAS only; pitv_content copies it to the cache before it airs">NAS only</span>
{/if}
{#if item?.cached && item?.origin === 'online'}<span class="badge info" title="Fetched online by pitv_content">online</span>{/if}
