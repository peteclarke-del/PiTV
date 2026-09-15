<script>
  // A file's video codec and whether the Pi can decode it in hardware (catalogue item, episode or music video).
  let { item } = $props();
</script>

<span class="mono">{item?.vcodec ?? '?'}</span>
{#if item?.hwdec}<span class="badge ok">HW</span>{:else}<span class="badge warn" title="pitv_content transcodes this file to an H.264 copy when it is scheduled">SW</span>{/if}
