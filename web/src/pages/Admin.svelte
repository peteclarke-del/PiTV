<script>
  import { onMount } from 'svelte';
  import { auth, route, toast } from '../lib/stores.svelte.js';
  import { post, tryApi, refreshAuth } from '../lib/api.js';
  import AppBadge from '../components/AppBadge.svelte';
  import Login from './admin/Login.svelte';
  import Dashboard from './admin/Dashboard.svelte';
  import Sources from './admin/Sources.svelte';
  import Library from './admin/Library.svelte';
  import Channels from './admin/Channels.svelte';
  import Settings from './admin/Settings.svelte';
  import LevelSwitch from '../components/LevelSwitch.svelte';
  import { shown } from '../lib/prefs.svelte.js';
  import Schedule from './admin/Schedule.svelte';
  import PlayerPage from './admin/Player.svelte';
  import System from './admin/System.svelte';
  import Wanted from './admin/Wanted.svelte';
  import Logs from './admin/Logs.svelte';
  import Content from './admin/Content.svelte';
  import Providers from './admin/Providers.svelte';

  // Two applications share this admin: PiTV (catalogue, line-ups, schedule, playback) and pitv_content
  // (sources, providers, fetching, encoding). The navigation keeps them apart so it is clear which app a
  // page changes. Each section carries the familiarity level it belongs to; the current one always shows.
  const groups = [
    ['pitv', 'Schedules and plays: what is on each channel and when',
      [['dashboard', 'Dashboard', 'basic'], ['channels', 'Channels', 'basic'], ['library', 'Catalogue', 'basic'],
       ['schedule', 'Schedule', 'basic'], ['settings', 'Settings', 'basic'], ['player', 'Player', 'standard'],
       ['logs', 'Logs', 'advanced'], ['system', 'System', 'basic']]],
    ['content', 'Indexes the NAS, fetches and encodes: what can be played',
      [['content', 'Content', 'basic'], ['sources', 'Sources', 'standard'], ['wanted', 'Wanted', 'standard'],
       ['providers', 'Providers', 'advanced']]],
  ];
  const ALIASES = { weighting: 'settings' };   // the Settings page's old address
  let tab = $derived(ALIASES[route.parts[1]] ?? route.parts[1] ?? 'dashboard');
  // "Skip for now" lasts for this browser session. Storage can be unavailable (private windows,
  // blocked site data); the choice then lasts until the page is reloaded.
  const SKIP_KEY = 'pitv-skip-setup';
  let skipSetup = $state(readSkip());
  function readSkip() {
    try { return sessionStorage.getItem(SKIP_KEY) === '1'; } catch { return false; }
  }
  let gate = $derived(!auth.checked ? 'loading' : auth.password_set && !auth.admin ? 'login' : !auth.password_set && !skipSetup ? 'setup' : 'ok');

  onMount(refreshAuth);

  async function logout() {
    if (await tryApi(post('/api/auth/logout'))) { toast.info('Logged out'); await refreshAuth(); }
  }
  function skip() {
    try { sessionStorage.setItem(SKIP_KEY, '1'); } catch { /* see SKIP_KEY */ }
    skipSetup = true;
  }
</script>

<div class="page admin-page">
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
      <span class="spacer"></span>
      <LevelSwitch />
    </div>
    <nav class="admin-nav" aria-label="Admin sections">
      {#each groups as [app, blurb, items] (app)}
        <div class="navgroup {app}">
          <AppBadge {app} title={blurb} />
          <div class="tabs">
            {#each items.filter(([id, , level]) => shown(level) || id === tab) as [id, name] (id)}
              <a href="#/admin/{id}" class:active={tab === id}>{name}</a>
            {/each}
          </div>
        </div>
      {/each}
    </nav>
    {#if tab === 'dashboard'}<Dashboard />
    {:else if tab === 'sources'}<Sources />
    {:else if tab === 'library'}<Library />
    {:else if tab === 'channels'}<Channels />
    {:else if tab === 'settings'}<Settings />
    {:else if tab === 'schedule'}<Schedule />
    {:else if tab === 'wanted'}<Wanted />
    {:else if tab === 'providers'}<Providers />
    {:else if tab === 'player'}<PlayerPage />
    {:else if tab === 'content'}<Content />
    {:else if tab === 'logs'}<Logs />
    {:else if tab === 'system'}<System />
    {:else}<div class="empty">Unknown section.</div>{/if}
  {/if}
</div>

<style>
  /* Admin tables, forms and the schedule benefit from the whole browser width. */
  .admin-page { width: 100%; max-width: none; padding-inline: clamp(12px, 2vw, 32px); }
  .admin-nav { display: flex; flex-wrap: wrap; gap: .4rem 1.2rem; margin-bottom: 1rem; border-bottom: 1px solid var(--border); }
  .navgroup { display: flex; align-items: center; gap: .5rem; min-width: 0; max-width: 100%; }
  .navgroup .tabs { border-bottom: 0; margin-bottom: 0; }
  .navgroup.pitv .tabs a.active { border-bottom-color: var(--app-pitv); }
  .navgroup.content .tabs a.active { border-bottom-color: var(--app-content); }
  .navgroup.content { padding-left: 1.2rem; border-left: 1px solid var(--border); }
  @media (max-width: 700px) { .navgroup.content { padding-left: 0; border-left: 0; } .navgroup { width: 100%; } }
</style>
