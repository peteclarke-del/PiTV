<script>
  let { channel, size = 'md', name = true } = $props();

  function textColour(hex) {
    if (!hex || hex.length < 7) return '#fff';
    const r = parseInt(hex.slice(1, 3), 16), g = parseInt(hex.slice(3, 5), 16), b = parseInt(hex.slice(5, 7), 16);
    const l = (0.299 * r + 0.587 * g + 0.114 * b) / 255;
    return l > 0.62 ? '#111' : '#fff';
  }
</script>

<span class="cb {size}" title={channel?.name}>
  <span class="num" style="background:{channel?.colour || '#888'};color:{textColour(channel?.colour)}">{channel?.number ?? '?'}</span>
  {#if name}<span class="name truncate">{channel?.name ?? ''}</span>{/if}
</span>

<style>
  .cb { display: inline-flex; align-items: center; gap: .45rem; min-width: 0; }
  .num {
    display: inline-flex; align-items: center; justify-content: center;
    font-weight: 800; font-variant-numeric: tabular-nums;
    width: 1.9rem; height: 1.9rem; border-radius: 6px; font-size: .95rem; flex: none;
  }
  .name { font-weight: 600; }
  .sm .num { width: 1.45rem; height: 1.45rem; font-size: .78rem; border-radius: 4px; }
  .sm .name { font-size: .85rem; }
  .lg .num { width: 2.6rem; height: 2.6rem; font-size: 1.3rem; border-radius: 8px; }
  .lg .name { font-size: 1.1rem; }
</style>
