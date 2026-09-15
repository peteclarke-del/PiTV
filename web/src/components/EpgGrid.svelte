<script>
  // Horizontal EPG grid: channels as rows, time across. Scrolls inside its own container.
  import { fmtTime, fmtRange, plural } from '../lib/format.js';
  import ChannelBadge from './ChannelBadge.svelte';

  let { channels = [], slots = [], start = 0, end = 0, now = 0, ppm = 4, selectedId = null,
        editable = false, onselect, loading = false } = $props();

  const CH_W = 92;
  let el = $state(null);
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
  let ticks = $derived.by(() => {
    const out = [];
    const first = Math.ceil(start / step) * step;
    for (let t = first; t < end; t += step) out.push(t);
    return out;
  });
  const x = (ts) => ((ts - start) / 60) * ppm;
  let nowX = $derived(now >= start && now <= end ? x(now) : null);

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
  function slotClass(s) {
    const c = [s.kind];
    if (s.block) c.push('music');
    if (s.end_ts <= now) c.push('past');
    else if (s.start_ts <= now) c.push('current');
    if (s.locked) c.push('locked');
    if (s.replay) c.push('replay');
    if (s.id === selectedId) c.push('selected');
    if (editable && s.start_ts > now && !s.replay) c.push('editable');
    return c.join(' ');
  }
</script>

<div class="epg" bind:this={el} class:loading style="--chw:{CH_W}px">
  <div class="head">
    <div class="corner"></div>
    <div class="axis" style="width:{width}px">
      {#each ticks as t (t)}
        <span class="tick" style="left:{x(t)}px">{fmtTime(t)}</span>
      {/each}
      {#if nowX !== null}<i class="nowmark" style="left:{nowX}px"></i>{/if}
    </div>
  </div>
  {#each channels as ch (ch.id)}
    <div class="chrow">
      <div class="chcell"><ChannelBadge channel={ch} size="sm" /></div>
      <div class="track" style="width:{width}px">
        {#each byChannel.get(ch.id) ?? [] as s (s.id)}
          {@const w = Math.max(2, x(s.end_ts) - x(s.start_ts))}
          <button class="slot {slotClass(s)}" style={slotStyle(s, w)}
                  onclick={() => onselect?.(s)}
                  title="{s.title} {s.subtitle ? `– ${s.subtitle}` : ''} ({fmtRange(s.start_ts, s.end_ts)})">
            <span class="inner">
              <span class="t truncate">{#if s.locked}<span class="lock">🔒</span>{/if}{s.kind === 'filler' ? (s.title || 'Filler') : s.kind === 'advert' ? (s.title || 'Advert') : s.kind === 'ident' ? 'Ident' : s.title}</span>
              {#if w > 70}<span class="st truncate">{fmtTime(s.start_ts)}{s.block && s.items ? ` · ${plural(s.items, 'video')}` : s.block && !s.items ? ` · ${s.block}` : s.subtitle ? ` · ${s.subtitle}` : ''}</span>{/if}
            </span>
          </button>
        {/each}
        {#if nowX !== null}<i class="nowline" style="left:{nowX}px"></i>{/if}
      </div>
    </div>
  {/each}
  {#if !channels.length}
    <div class="empty">{loading ? 'Loading…' : 'No channels are enabled.'}</div>
  {/if}
</div>

<style>
  .epg {
    position: relative; overflow: auto; border: 1px solid var(--border); border-radius: var(--radius);
    background: var(--bg-elev); max-height: calc(100vh - 220px); min-height: 200px;
    -webkit-overflow-scrolling: touch; overscroll-behavior-x: contain;
  }
  .epg.loading { opacity: .6; }
  .head { display: flex; flex-wrap: nowrap; position: sticky; top: 0; z-index: 3; background: var(--bg-elev); border-bottom: 1px solid var(--border); height: 30px; }
  .corner { position: sticky; left: 0; z-index: 4; width: var(--chw); flex: none; background: var(--bg-elev); border-right: 1px solid var(--border); }
  .axis { position: relative; flex: none; height: 100%; }
  .tick { position: absolute; top: 0; bottom: 0; display: flex; align-items: center; padding-left: 4px; font-size: .74rem; color: var(--fg-muted); border-left: 1px solid var(--border); font-variant-numeric: tabular-nums; white-space: nowrap; }
  .nowmark { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent); }
  .nowmark::after { content: ''; position: absolute; top: 0; left: -4px; border: 5px solid transparent; border-top: 7px solid var(--accent); }
  .chrow { display: flex; flex-wrap: nowrap; border-bottom: 1px solid var(--border); }
  .chcell { position: sticky; left: 0; z-index: 2; width: var(--chw); flex: none; background: var(--bg-elev); border-right: 1px solid var(--border); padding: .35rem .4rem; display: flex; align-items: center; overflow: hidden; }
  .track { position: relative; height: 54px; flex: none; overflow: hidden; background: repeating-linear-gradient(90deg, transparent 0, transparent calc(var(--gridstep, 60px) - 1px), var(--border) calc(var(--gridstep, 60px) - 1px), var(--border) var(--gridstep, 60px)); }
  .nowline { position: absolute; top: 0; bottom: 0; width: 2px; background: var(--accent); pointer-events: none; z-index: 1; }
  .slot {
    position: absolute; top: 4px; bottom: 4px; border-radius: 5px; padding: 0; overflow: hidden;
    display: block; text-align: left; font-weight: 500; min-height: 0; border: 1px solid var(--border-strong);
    background: var(--bg-sunken); color: var(--fg); transition: none;
  }
  .slot .inner { position: sticky; left: var(--chw); display: flex; flex-direction: column; padding: .3rem .45rem; max-width: 100%; }
  .slot .t { font-size: .84rem; line-height: 1.2; }
  .slot .st { font-size: .72rem; color: var(--fg-muted); }
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
  .empty { padding: 2rem; }
</style>
