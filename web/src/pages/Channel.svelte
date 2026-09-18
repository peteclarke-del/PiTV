<script>
  // Watch a channel in the browser: the same programme, at the same moment, as the television.
  // Safari and iOS play the playlist natively; everything else needs hls.js, which is bundled.
  import { onMount } from 'svelte';
  import { get, tryApi } from '../lib/api.js';
  import { clock, route } from '../lib/stores.svelte.js';
  import { navigate } from '../lib/router.js';
  import { poll } from '../lib/poll.svelte.js';
  import { fmtRange } from '../lib/format.js';
  import ChannelBadge from '../components/ChannelBadge.svelte';
  import ProgressBar from '../components/ProgressBar.svelte';

  let number = $derived(Number(route.parts[1] ?? 1));
  let now = $state(null);            // GET /api/now, for the strip under the picture
  let video = $state(null);
  let error = $state('');
  let loading = $state(true);
  // A browser only starts a video on its own when it is silent, so the picture comes up muted
  // and the viewer turns the sound on. One click, and it stays on for the rest of the visit.
  let muted = $state(true);
  // One id per browser player lets the server retire this player's previous channel without
  // confusing it with another tab (localhost viewers otherwise all have the same address).
  const viewer = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
  function sound() {
    if (!video) return;
    video.muted = false;
    video.volume = 1;
    muted = false;
    video.play?.().catch(() => {});
  }
  let entry = $derived((now?.channels ?? []).find((c) => c.channel.number === number) ?? null);
  let others = $derived((now?.channels ?? []).map((c) => c.channel));

  async function load() { now = (await tryApi(get('/api/now', { next: 1 }))) ?? now; }
  onMount(load);
  poll(load, 20000);
  // One player per channel: rebuilt when the channel changes, torn down when the page closes.
  $effect(() => {
    const src = `/channel/${number}.m3u8?viewer=${encodeURIComponent(viewer)}`;
    const el = video;
    if (!el) return;
    error = '';
    loading = true;
    let cancelled = false;
    let instance = null;
    let began = false;
    const beganPlaying = () => { began = true; };
    const ensurePlaying = () => {
      if (!cancelled && !began && el.paused && el.readyState >= 2) el.play?.().catch(() => {});
    };
    el.addEventListener('playing', beganPlaying);
    const playRetry = setInterval(ensurePlaying, 500);
    (async () => {
      const { default: Hls } = await import('hls.js');
      if (cancelled) return;
      // Modern Chromium reports native HLS as "maybe" but its Linux MPEG-TS demuxer rejects
      // these otherwise valid streams. Prefer hls.js whenever MediaSource is available; native
      // HLS remains the fallback for Safari and other browsers without MSE support.
      if (!Hls.isSupported()) {
        if (el.canPlayType('application/vnd.apple.mpegurl')) {
          el.src = src;
          el.play?.().catch(() => {});
        } else {
          error = 'This browser cannot play the stream.';
        }
        return;
      }
      instance = new Hls({ liveDurationInfinity: true, lowLatencyMode: false });
      instance.on(Hls.Events.MANIFEST_PARSED, () => el.play?.().catch(() => {}));
      instance.on(Hls.Events.FRAG_BUFFERED, () => el.play?.().catch(() => {}));
      instance.on(Hls.Events.ERROR, (_e, data) => {
        if (!data.fatal) return;
        console.error('PiTV HLS fatal error', {
          type: data.type, details: data.details, reason: data.reason,
          error: data.error?.message, url: data.frag?.url,
        });
        error = data.response?.code === 503
          ? 'PiTV is not streaming this channel: streaming may be off, or too many streams are running.'
          : 'The stream stopped. Trying again.';
        if (data.type === Hls.ErrorTypes.NETWORK_ERROR) instance.startLoad();
        else instance.recoverMediaError();
      });
      instance.loadSource(src);
      instance.attachMedia(el);
    })();
    return () => {
      cancelled = true;
      clearInterval(playRetry);
      el.removeEventListener('playing', beganPlaying);
      instance?.destroy();
      el.pause();
      el.removeAttribute('src');
      el.load();
      fetch(src, { method: 'DELETE', keepalive: true }).catch(() => {});
    };
  });
</script>

<div class="page stack">
  <div class="row">
    {#each others as c (c.id)}
      <button class="chip" class:on={c.number === number} onclick={() => navigate(`/channel/${c.number}`)}>
        <ChannelBadge channel={c} size="sm" />
      </button>
    {/each}
  </div>

  <!-- svelte-ignore a11y_media_has_caption -->
  <div class="picture">
    <video bind:this={video} controls autoplay playsinline muted
      onloadstart={() => (loading = true)} onwaiting={() => (loading = true)}
      onplaying={() => (loading = false)} onloadeddata={() => video?.play?.().catch(() => {})}
      onvolumechange={() => (muted = !!video?.muted)}></video>
    {#if loading && !error}<div class="starting">Starting {entry?.channel?.name ?? `channel ${number}`}…</div>{/if}
  </div>
  {#if muted}
    <button class="sound" onclick={sound}>Sound is off. Turn it on</button>
  {/if}

  {#if error}<div class="badge danger">{error}</div>{/if}

  {#if entry}
    <div class="card">
      <div class="card-title"><h3>{entry.now?.title ?? 'Off air'}</h3><ChannelBadge channel={entry.channel} /></div>
      {#if entry.now}
        <p class="small">{fmtRange(entry.now.start_ts, entry.now.end_ts)}{entry.now.subtitle ? ` · ${entry.now.subtitle}` : ''}</p>
        <ProgressBar start={entry.now.start_ts} end={entry.now.end_ts} now={clock.ts} />
      {/if}
      {#if entry.next?.length}<p class="small muted">Next {fmtRange(entry.next[0].start_ts, entry.next[0].end_ts)} {entry.next[0].title}</p>{/if}
    </div>
  {/if}

  <p class="small muted">In VLC or on a smart TV, open <span class="mono">{location.origin}/channel/{number}.m3u8</span>. The stream follows the schedule, so it shows what the television shows, about fifteen seconds behind.</p>
</div>

<style>
  .picture { position: relative; }
  video { display: block; width: 100%; max-height: 70vh; background: #000; border-radius: var(--radius-sm); aspect-ratio: 4 / 3; }
  .starting { position: absolute; inset: 0; display: grid; place-items: center; pointer-events: none;
    color: #fff; background: rgb(0 0 0 / .42); font-weight: 700; text-shadow: 0 1px 3px #000; }
  .sound { align-self: flex-start; }
  .chip { cursor: pointer; background: none; border: 1px solid transparent; padding: .1rem; border-radius: 999px; }
  .chip.on { border-color: var(--accent); }
</style>
