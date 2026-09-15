<script>
  let { size = 'md' } = $props();
</script>

<span class="logo {size}" role="img" aria-label="PiTV">
  <span class="word">Pi<span class="tv">TV</span></span>
  <span class="bars" aria-hidden="true"><i></i><i></i><i></i><i></i></span>
</span>

<style>
  .logo { display: inline-flex; flex-direction: column; line-height: 1; user-select: none; }
  .word {
    font-weight: 900; font-style: italic; letter-spacing: -.05em; font-size: 1.35rem;
    color: var(--fg);
    text-shadow: 2px 2px 0 color-mix(in srgb, var(--accent) 55%, transparent);
  }
  .tv { color: var(--accent); }
  .bars { display: flex; gap: 2px; margin-top: 3px; height: 3px; }
  .bars i { flex: 1; border-radius: 1px; }
  .bars i:nth-child(1) { background: #e63946; }
  .bars i:nth-child(2) { background: #f4a261; }
  .bars i:nth-child(3) { background: #2a9d8f; }
  .bars i:nth-child(4) { background: #457b9d; }
  .lg .word { font-size: 2.4rem; }
</style>
