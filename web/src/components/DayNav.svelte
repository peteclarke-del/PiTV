<script>
  // Previous / day picker / next for the guide and the schedule editor. `days` is GET /api/schedule/days.
  import { fmtDay } from '../lib/format.js';

  let { days = null, day = '', onpick } = $props();

  function shift(n) {
    const i = days.days.findIndex((d) => d.day === day);
    const t = days.days[i + n];
    if (t) onpick?.(t.day);
  }
</script>

<button class="small" onclick={() => shift(-1)} disabled={!days || days.days[0]?.day === day} aria-label="Previous day">◀</button>
<select value={day} onchange={(e) => onpick?.(e.currentTarget.value)} aria-label="Day">
  {#each days?.days ?? [] as d (d.day)}
    <option value={d.day}>{fmtDay(d.day)}{d.day === days.today ? ' (today)' : ''}</option>
  {/each}
</select>
<button class="small" onclick={() => shift(1)} disabled={!days || days.days.at(-1)?.day === day} aria-label="Next day">▶</button>
