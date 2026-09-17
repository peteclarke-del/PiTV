<script>
  import { onMount } from 'svelte';
  import { startRouter } from './lib/router.js';
  import { route, clock } from './lib/stores.svelte.js';
  import { connectEvents } from './lib/api.js';
  import { fmtTime } from './lib/format.js';
  import { poll } from './lib/poll.svelte.js';
  import Logo from './components/Logo.svelte';
  import Toast from './components/Toast.svelte';
  import ConfirmDialog from './components/ConfirmDialog.svelte';
  import Now from './pages/Now.svelte';
  import Guide from './pages/Guide.svelte';
  import RemotePage from './pages/RemotePage.svelte';
  import Admin from './pages/Admin.svelte';
  import Channel from './pages/Channel.svelte';

  const links = [
    ['/', 'Now'],
    ['/guide', 'Guide'],
    ['/remote', 'Remote'],
    ['/admin', 'Admin'],
  ];
  let section = $derived(route.parts[0] ?? '');

  onMount(() => {
    startRouter();
    connectEvents();
  });
  poll(() => { clock.ts = Math.floor(Date.now() / 1000); }, 1000);
</script>

<header class="top">
  <a href="#/" class="brand"><Logo /></a>
  <nav>
    {#each links as [path, label] (path)}
      <a href="#{path}" class:active={path === '/' ? section === '' : section === path.slice(1)}>{label}</a>
    {/each}
  </nav>
  <span class="clock mono" aria-label="Current time">{fmtTime(clock.ts)}</span>
</header>

<main>
  {#if section === ''}
    <Now />
  {:else if section === 'guide'}
    <Guide />
  {:else if section === 'remote'}
    <RemotePage />
  {:else if section === 'admin'}
    <Admin />
  {:else if section === 'channel' || section === 'channels'}
    <Channel />
  {:else}
    <div class="page"><div class="empty">Page not found. <a href="#/">Go to Now &amp; Next</a></div></div>
  {/if}
</main>

<Toast />
<ConfirmDialog />

<style>
  .top {
    position: sticky; top: 0; z-index: 50; height: var(--nav-h);
    display: flex; align-items: center; gap: 1rem; padding: 0 16px;
    background: color-mix(in srgb, var(--bg-elev) 88%, transparent); backdrop-filter: blur(10px);
    border-bottom: 1px solid var(--border);
  }
  .brand { display: inline-flex; text-decoration: none; }
  nav { display: flex; gap: .15rem; flex: 1; overflow-x: auto; scrollbar-width: none; }
  nav::-webkit-scrollbar { display: none; }
  nav a {
    padding: .35rem .7rem; border-radius: 999px; color: var(--fg-muted); font-weight: 600; font-size: .92rem; white-space: nowrap;
  }
  nav a:hover { color: var(--fg); text-decoration: none; background: var(--bg-sunken); }
  nav a.active { color: var(--fg); background: var(--bg-sunken); }
  .clock { font-size: 1rem; font-weight: 600; color: var(--fg-muted); letter-spacing: .04em; font-variant-numeric: tabular-nums; }
  @media (max-width: 480px) { .clock { display: none; } .top { gap: .6rem; } }
</style>
