<script module>
  export const APP_LABEL = { pitv: 'PiTV', content: 'pitv_content' };
</script>

<script>
  // Which application owns a setting, card or page. PiTV schedules and plays; pitv_content indexes, fetches and encodes.
  let { app = 'pitv', title = '' } = $props();
  const TIP = {
    pitv: 'PiTV: changes what PiTV schedules, plays or shows',
    content: 'pitv_content: owned by the support app; PiTV shows it and relays edits through its API',
  };
</script>

<span class="app {app}" title={title || TIP[app]}>{APP_LABEL[app] ?? app}</span>
