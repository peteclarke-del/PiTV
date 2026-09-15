<script>
  import { post } from '../../lib/api.js';
  import Logo from '../../components/Logo.svelte';

  let { mode = 'login', ondone, onskip } = $props();
  let password = $state('');
  let confirmPw = $state('');
  let error = $state('');
  let busy = $state(false);

  async function submit(e) {
    e.preventDefault();
    error = '';
    if (mode === 'setup' && password !== confirmPw) { error = 'Passwords do not match'; return; }
    busy = true;
    try {
      await post(mode === 'setup' ? '/api/auth/setup' : '/api/auth/login', { password });
      password = confirmPw = '';
      ondone?.();
    } catch (err) {
      error = err.detail || err.message;
    }
    busy = false;
  }
</script>

<div class="login card">
  <div class="center mb"><Logo size="lg" /></div>
  {#if mode === 'setup'}
    <h2>Set an admin password</h2>
    <p class="muted small">No password has been set yet, so the admin area is open to anyone on your network. Choose one now (at least 6 characters).</p>
  {:else}
    <h2>Admin login</h2>
    <p class="muted small">Enter the admin password to manage sources, channels and the schedule.</p>
  {/if}
  <form onsubmit={submit} class="stack">
    <label class="field">Password
      <input type="password" bind:value={password} autocomplete={mode === 'setup' ? 'new-password' : 'current-password'} minlength="6" required />
    </label>
    {#if mode === 'setup'}
      <label class="field">Confirm password
        <input type="password" bind:value={confirmPw} autocomplete="new-password" minlength="6" required />
      </label>
    {/if}
    {#if error}<div class="badge danger" role="alert">{error}</div>{/if}
    <div class="row">
      <button class="primary" type="submit" disabled={busy}>{mode === 'setup' ? 'Set password' : 'Log in'}</button>
      {#if mode === 'setup'}
        <button type="button" class="ghost" onclick={onskip}>Skip for now</button>
      {/if}
    </div>
    {#if mode === 'setup'}
      <div class="warn-box">Skipping leaves the admin area unprotected until a password is set in System.</div>
    {/if}
  </form>
</div>

<style>
  .login { max-width: 420px; margin: 2rem auto; }
</style>
