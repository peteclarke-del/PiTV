<script>
  // The token pitv_content presents on PiTV's manifest, report and make-room endpoints. On the
  // same Pi it reads the file itself; this card is for a pitv_content on another machine.
  import { confirmApi, get, post, tryApi } from '../../lib/api.js';
  import { toast } from '../../lib/stores.svelte.js';
  import AppBadge from '../../components/AppBadge.svelte';

  let info = $state(null);
  let shown = $state(false);

  async function reveal() {
    info = (await tryApi(get('/api/content/token'))) ?? info;
    shown = !!info;
  }
  async function copy() {
    if (!info) await reveal();
    if (!info) return;
    try { await navigator.clipboard.writeText(info.token); toast.success('Token copied'); }
    catch { toast.error('The browser refused the clipboard; select the token and copy it by hand'); }
  }
  const rotate = () => confirmApi('Issue a new token? pitv_content is refused until it has the new one; on the same Pi it picks it up from the file on its next call.',
    { title: 'New token', okLabel: 'Issue new token', danger: true },
    async () => { info = await post('/api/content/token'); shown = true; return info; }, { success: 'New token issued' });
</script>

<div class="card">
  <div class="card-title"><h3>pitv_content token</h3><AppBadge app="pitv" />
    <button class="small" onclick={() => (shown ? (shown = false) : reveal())}>{shown ? 'Hide' : 'Show'}</button>
    <button class="small" onclick={copy}>Copy</button>
    <button class="small danger" onclick={rotate}>New token</button>
  </div>
  <p class="scope">pitv_content sends this with every manifest, report and make-room request, since it has no admin login. On the same Pi it reads the token file itself; on another machine, give it this value (Show reveals both).</p>
  {#if shown && info}
    <div class="mono small">{info.token}</div>
    <div class="tiny muted mono mt">{info.file}</div>
  {/if}
</div>
