<script>
  // Horizontal EPG grid: channels as rows, time across. Scrolls inside its own container.
  //
  // The grid sizes itself to the space it is given. Its scale was fixed at four pixels a minute
  // and its rows at 54 pixels, so on a desktop monitor it showed six hours of cramped blocks in
  // the top half of the window with the channel names cut to "PiTV ...". The width decides how
  // many hours are on screen and whether there is room for the channel names; the height left
  // in the window is shared between the rows.
  import { fmtTime, fmtRange, plural } from '../lib/format.js';
  import ChannelBadge from './ChannelBadge.svelte';

  let { channels = [], slots = [], start = 0, end = 0, now = 0, selectedId = null,
        editable = false, onselect, loading = false, follow = false } = $props();

  const MIN_PPM = 3, MAX_PPM = 9;          // pixels per minute: below, titles do not fit; above, a film fills the screen
  const ROW_MIN = 54, ROW_MAX = 104;       // pixels: two lines of text, up to a wrapped title and its details
  const HEAD_H = 30, FOOT = 80;            // the time axis, and what the page keeps below the grid
  let el = $state(null);
  let boxW = $state(0);
  let winH = $state(0);
  let top = $state(0);                     // where the grid starts in the window
  $effect(() => {
    winH; boxW; channels.length;           // measured again when any of these changes
    if (el) top = el.getBoundingClientRect().top + window.scrollY;
  });
  // Channel names need about 170 pixels; a phone has none to spare and shows the number alone.
  let named = $derived(boxW >= 700);
  let chW = $derived(named ? 176 : 52);
  // Hours across the visible track, by how much there is to look at them in.
  let hours = $derived(boxW < 600 ? 2 : boxW < 1000 ? 3 : boxW < 1500 ? 4 : boxW < 2200 ? 5 : 6);
  let ppm = $derived(boxW ? Math.min(MAX_PPM, Math.max(MIN_PPM, (boxW - chW) / (hours * 60))) : 4);
  let rowH = $derived(channels.length && winH
    ? Math.round(Math.min(ROW_MAX, Math.max(ROW_MIN, (winH - top - HEAD_H - FOOT) / channels.length)))
    : ROW_MIN);
  let tall = $derived(rowH >= 76);
  let roomy = $derived(rowH >= 100);      // room for a third line under a title on two
  let width = $derived(Math.max(0, ((end - start) / 60) * ppm));
  let byChannel = $derived.by(() => {
    const m = new Map();
    for (const s of slots) {
      if (!m.has(s.channel_id)) m.set(s.channel_id, []);
      m.get(s.channel_id).push(s);
    }
    return m;
  });
  let step = $derived(ppm * 30 >= 72 ? 1800 : 3600);
  let firstTick = $derived(Math.ceil(start / step) * step);
  let ticks = $derived.by(() => {
    const out = [];
    for (let t = firstTick; t < end; t += step) out.push(t);
    return out;
  });
  const x = (ts) => ((ts - start) / 60) * ppm;
  // Each track's background is one tile per tick step, ending in a 1px line and shifted to the first tick.
  let gridStyle = $derived(`--gridstep:${(step / 60) * ppm}px;--grid0:${x(firstTick)}px`);
  // The now-line moves every second through one CSS variable on the root; the past/current classes on
  // every slot only need re-evaluating when the clock crosses a 30 s boundary.
  let nowX = $derived(now >= start && now <= end ? x(now) : null);
  let nowCoarse = $derived(Math.floor(now / 30) * 30);

  // With `follow`, the present stays in sight: when the now-line has gone off either side, or
  // into the last fifth of what is visible, the grid moves on. Never while somebody is using
  // it: a grid that scrolls under a reader's finger is worse than one that falls behind.
  const IDLE_SECONDS = 90;
  let touched = 0;
  const touch = () => { touched = Date.now(); };
  $effect(() => {
    nowCoarse;
    if (!follow || !el || nowX === null || Date.now() - touched < IDLE_SECONDS * 1000) return;
    const visible = el.clientWidth - chW;
    if (nowX < el.scrollLeft || nowX > el.scrollLeft + visible * 0.8) scrollTo(now, Math.round(visible * 0.15));
  });

  export function scrollTo(ts, pad = 40) {
    if (el) el.scrollTo({ left: Math.max(0, x(ts) - pad), behavior: 'smooth' });
  }
  export function scrollByMinutes(min) {
    if (el) el.scrollBy({ left: min * ppm, behavior: 'smooth' });
  }

  function hue(name) { let h = 0; for (const ch of name) h = (h * 31 + ch.charCodeAt(0)) % 360; return h; }
  function slotStyle(s, w) {
    let st = `left:${x(s.start_ts)}px;width:${w}px`;
    if (s.block) st += `;--blk:${hue(s.block)}`;
    return st;
  }
  function slotClass(s, t) {
    const c = [s.kind];
    if (s.block) c.push('music');
    if (s.end_ts <= t) c.push('past');
    else if (s.start_ts <= t) c.push('current');
    if (s.locked) c.push('locked');
    if (s.replay) c.push('replay');
    if (s.id === selectedId) c.push('selected');
    if (editable && s.start_ts > t && !s.replay) c.push('editable');
    return c.join(' ');
  }
  const slotTitle = (s) => s.kind === 'filler' ? (s.title || 'Filler') : s.kind === 'advert' ? (s.title || 'Advert') : s.kind === 'ident' ? 'Ident' : s.title;
  /** The programme on in a band at `t`: the last of its parts to have started. */
  const onNow = (s, t) => (s.parts ?? []).findLast((p) => p.start_ts <= t);
  const slotSub = (s) => (s.block && s.items > 1 ? ` · ${plural(s.items, 'programme')}` : s.subtitle ? ` · ${s.subtitle}` : s.block ? ` · ${s.block}` : '');
</script>

<svelte:window bind:innerHeight={winH} />

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="epg" bind:this={el} bind:clientWidth={boxW} onpointerdown={touch} onwheel={touch} ontouchstart={touch} onkeydown={touch} class:loading class:tall class:hasnow={nowX !== null}
     style="--chw:{chW}px;--rowh:{rowH}px;--nowx:{nowX ?? 0}px;{gridStyle}">
  <div class="head">
    <div class="corner"></div>
    <div class="axis" style="width:{width}px">
      {#each ticks as t (t)}
        <span class="tick" style="left:{x(t)}px">{fmtTime(t)}</span>
      {/each}
      <i class="nowmark"></i>
    </div>
  </div>
  <div class="body">
    {#each channels as ch (ch.id)}
      <div class="chrow">
        <div class="chcell"><ChannelBadge channel={ch} size={tall ? 'md' : 'sm'} name={named} /></div>
        <div class="track" style="width:{width}px">
          {#each byChannel.get(ch.id) ?? [] as s (s.id)}
            {@const w = Math.max(2, x(s.end_ts) - x(s.start_ts))}
            <button class="slot {slotClass(s, nowCoarse)}" style={slotStyle(s, w)}
                    onclick={() => onselect?.(s)}
                    title="{s.title}{s.subtitle ? `: ${s.subtitle}` : ''} ({fmtRange(s.start_ts, s.end_ts)})">
              <span class="inner">
                <span class="t truncate">{#if s.locked}<span class="lock" aria-label="Locked">🔒</span>{/if}{slotTitle(s)}</span>
                {#if w > 70}<span class="st truncate">{fmtTime(s.start_ts)}{slotSub(s)}</span>{/if}
                {#if w > 70 && s.items > 1 && s.start_ts <= now && s.end_ts > now}
                  {@const part = onNow(s, now)}
                  {#if part}<span class="st on truncate">Now: {part.title}{part.subtitle ? ` · ${part.subtitle}` : ''}</span>{/if}
                {/if}
                {#if roomy && w > 150 && s.kind === 'programme' && !s.block}<span class="st truncate">until {fmtTime(s.end_ts)}{s.certificate ? ` · ${s.certificate}` : ''}</span>{/if}
              </span>
            </button>
          {/each}
        </div>
      </div>
    {/each}
    <i class="nowline"></i>
  </div>
  {#if !channels.length}
    <div class="empty">{loading ? 'Loading…' : 'No channels are enabled.'}</div>
  {/if}
</div>

<style>
  .epg {
    position: relative; overflow: auto; border: 1px solid var(--border); border-radius: var(--radius);
    background: var(--bg-elev); min-height: 200px;
    -webkit-overflow-scrolling: touch; overscroll-behavior-x: contain;
  }
  .epg.loading { opacity: .6; }
  /* The head and each row are as wide as the day, not as wide as the window. A sticky cell
     stays put only inside its own row's box: with rows the width of the window, the channel
     column stayed for the first screenful of scrolling and then left with the row. */
  .head, .chrow { width: max-content; min-width: 100%; }
  .head { display: flex; flex-wrap: nowrap; position: sticky; top: 0; z-index: 3; background: var(--bg-elev); border-bottom: 1px solid var(--border); height: 30px; }
  .corner { position: sticky; left: 0; z-index: 4; width: var(--chw); flex: none; background: var(--bg-elev); border-right: 1px solid var(--border); }
  .axis { position: relative; flex: none; height: 100%; }
  .tick { position: absolute; top: 0; bottom: 0; display: flex; align-items: center; padding-left: 4px; font-size: .74rem; color: var(--fg-muted); border-left: 1px solid var(--border); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .body { position: relative; }
  .nowmark, .nowline { display: none; position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent); pointer-events: none; }
  .hasnow .nowmark, .hasnow .nowline { display: block; }
  .nowmark { left: var(--nowx); }
  .nowmark::after { content: ''; position: absolute; top: 0; left: -4px; border: 5px solid transparent; border-top: 7px solid var(--accent); }
  .nowline { left: calc(var(--chw) + var(--nowx)); z-index: 1; }
  .chrow { display: flex; flex-wrap: nowrap; border-bottom: 1px solid var(--border); }
  .chcell { position: sticky; left: 0; z-index: 2; width: var(--chw); flex: none; background: var(--bg-elev); border-right: 1px solid var(--border); padding: .35rem .4rem; display: flex; align-items: center; overflow: hidden; }
  .track { position: relative; height: var(--rowh); flex: none; overflow: hidden; background: linear-gradient(90deg, transparent calc(100% - 1px), var(--border) 0) var(--grid0) 0 / var(--gridstep) 100%; }
  .slot {
    position: absolute; top: 4px; bottom: 4px; border-radius: 5px; padding: 0; overflow: hidden;
    display: block; text-align: left; font-weight: 500; min-height: 0; border: 1px solid var(--border-strong);
    background: var(--bg-sunken); color: var(--fg); transition: none;
  }
  .slot .inner { position: sticky; left: var(--chw); display: flex; flex-direction: column; padding: .3rem .45rem; max-width: 100%; }
  .slot .t { font-size: .84rem; line-height: 1.2; }
  .slot .st { font-size: .72rem; color: var(--fg-muted); }
  .slot .st.on { color: var(--fg); }
  /* A taller row has room for larger type and a title on two lines. */
  .tall .slot .inner { padding: .45rem .6rem; gap: .1rem; }
  .tall .slot .t { font-size: .95rem; white-space: normal; display: -webkit-box; -webkit-line-clamp: 2; line-clamp: 2; -webkit-box-orient: vertical; }
  .tall .slot .st { font-size: .8rem; }
  .tall .tick { font-size: .82rem; }
  .chcell :global(.cb) { justify-content: flex-start; width: 100%; }
  .slot.past { opacity: .55; }
  .slot.current { border-color: var(--accent); background: color-mix(in srgb, var(--accent) 12%, var(--bg-elev)); }
  .slot.advert { background: color-mix(in srgb, var(--warn) 22%, var(--bg-elev)); border-color: color-mix(in srgb, var(--warn) 50%, var(--border-strong)); }
  .slot.ident { background: color-mix(in srgb, var(--info) 22%, var(--bg-elev)); }
  .slot.filler { background: repeating-linear-gradient(45deg, var(--bg-sunken) 0 6px, var(--bg-elev) 6px 12px); color: var(--fg-muted); }
  .slot.music { background: color-mix(in srgb, hsl(var(--blk, 280) 60% 55%) 22%, var(--bg-elev)); border-color: color-mix(in srgb, hsl(var(--blk, 280) 60% 45%) 55%, var(--border-strong)); }
  .slot.replay { opacity: .7; border-style: dashed; }
  .slot.selected { outline: 2px solid var(--info); outline-offset: -1px; }
  .slot:hover { filter: brightness(.96); }
  .slot.editable { cursor: pointer; }
  .lock { font-size: .7rem; margin-right: .2rem; }
</style>
