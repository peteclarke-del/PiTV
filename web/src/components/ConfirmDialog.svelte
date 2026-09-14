<script>
  import Modal from './Modal.svelte';
  import { confirmState, answerConfirm } from '../lib/stores.svelte.js';
</script>

<Modal open={confirmState.open} title={confirmState.title} onclose={() => answerConfirm(false)}>
  <p style="white-space:pre-wrap">{confirmState.message}</p>
  {#snippet footer()}
    <button onclick={() => answerConfirm(false)}>Cancel</button>
    <button class={confirmState.danger ? 'danger' : 'primary'} onclick={() => answerConfirm(true)}>{confirmState.okLabel}</button>
  {/snippet}
</Modal>
