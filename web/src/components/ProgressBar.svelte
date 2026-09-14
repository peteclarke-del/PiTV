<script>
  let { value = 0 } = $props();
  let pct = $derived(Math.max(0, Math.min(100, value * 100)));
</script>

<div class="progress" role="progressbar" aria-valuenow={Math.round(pct)} aria-valuemin="0" aria-valuemax="100">
  <i style="width:{pct}%"></i>
</div>
