<script>
  import { onMount } from 'svelte';
  import { auth, route, toast } from '../lib/stores.svelte.js';
  import { post, refreshAuth } from '../lib/api.js';
  import Login from './admin/Login.svelte';
  import Dashboard from './admin/Dashboard.svelte';
  import Sources from './admin/Sources.svelte';
  import Library from './admin/Library.svelte';
  import Channels from './admin/Channels.svelte';
  import Weighting from './admin/Weighting.svelte';
  import Schedule from './admin/Schedule.svelte';
  import PlayerPage from './admin/Player.svelte';
  import System from './admin/System.svelte';
  import Wanted from './admin/Wanted.svelte';
  import Logs from './admin/Logs.svelte';
  import Content from './admin/Content.svelte';

  const tabs = [
    ['dashboard', 'Dashboard'], ['sources', 'Sources'], ['library', 'Library'], ['channels', 'Channels'],
    ['weighting', 'Weighting'], ['schedule', 'Schedule'], ['wanted', 'Wanted'], ['content', 'Content'], ['player', 'Player'], ['logs', 'Logs'], ['system', 'System'],
  ];
  let tab = $derived(route.parts[1] ?? 'dashboard');
  let skipSetup = $state(sessionStorage.getItem('pitv-skip-setup') === '1');
  let gate = $derived(!auth.checked ? 'loading' : auth.password_set && !auth.admin ? 'login' : !auth.password_set && !skipSetup ? 'setup' : 'ok');

  onMount(refreshAuth);

  async function logout() {
    await post('/api/auth/logout');
    toast.info('Logged out');
    await refreshAuth();
  }
  function skip() {
    sessionStorage.setItem('pitv-skip-setup', '1');
    skipSetup = true;
  }
</script>

<div class="page">
  {#if gate === 'loading'}
    <div class="skeleton" style="height:200px"></div>
  {:else if gate === 'login'}
    <Login mode="login" ondone={refreshAuth} />
  {:else if gate === 'setup'}
    <Login mode="setup" ondone={refreshAuth} onskip={skip} />
  {:else}
    <div class="page-head">
      <h1>Admin</h1>
      {#if auth.password_set}
        <button class="small ghost" onclick={logout}>Log out</button>
      {:else}
        <a class="badge warn" href="#/admin/system" title="Set a password in System">No admin password</a>
      {/if}
    </div>
    <nav class="tabs">
      {#each tabs as [id, label] (id)}
        <a href="#/admin/{id}" class:active={tab === id}>{label}</a>
      {/each}
    </nav>
    {#if tab === 'dashboard'}<Dashboard />
    {:else if tab === 'sources'}<Sources />
    {:else if tab === 'library'}<Library />
    {:else if tab === 'channels'}<Channels />
    {:else if tab === 'weighting'}<Weighting />
    {:else if tab === 'schedule'}<Schedule />
    {:else if tab === 'wanted'}<Wanted />
    {:else if tab === 'player'}<PlayerPage />
    {:else if tab === 'content'}<Content />
    {:else if tab === 'logs'}<Logs />
    {:else if tab === 'system'}<System />
    {:else}<div class="empty">Unknown section.</div>{/if}
  {/if}
</div>
