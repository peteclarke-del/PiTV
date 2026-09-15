<script>
  // An ordered subset of fixed choices, such as sources tried in turn: move, remove, add back.
  import { moveItem } from '../lib/util.js';

  let { value = $bindable([]), choices = [], label = '' } = $props();
  let missing = $derived(choices.filter((c) => !value.includes(c)));
</script>

<div class="row" role="group" aria-label={label}>
  {#each value as item, i (item)}
    <span class="chip"><button type="button" onclick={() => moveItem(value, i, -1)} disabled={i === 0} aria-label="Earlier">‹</button><b>{i + 1}. {item}</b><button type="button" onclick={() => moveItem(value, i, 1)} disabled={i === value.length - 1} aria-label="Later">›</button><button type="button" onclick={() => value.splice(i, 1)} aria-label="Remove {item}">✕</button></span>
  {:else}<span class="muted small">None</span>{/each}
  {#each missing as c (c)}<button type="button" class="small ghost" onclick={() => value.push(c)}>+ {c}</button>{/each}
</div>
