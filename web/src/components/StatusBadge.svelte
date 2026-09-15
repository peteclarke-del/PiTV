<script>
  // Coloured pill for a job / run / wanted status (ok, running, warning, failed, queued…).
  import { statusClass } from '../lib/format.js';

  let { status, label, title } = $props();
</script>

<span class="badge {statusClass(status)}" {title}>{label ?? status}</span>
