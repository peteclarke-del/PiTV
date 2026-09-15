<script>
  // What pitv_content found online for a title about to be added (contract section 8), so the
  // admin can pick the right one. Images and links come from outside sources: https images
  // only, no referrer, links open in a new tab without access back to this page.
  import { fmtDuration, safeUrl } from '../../lib/format.js';

  let { candidates = [], onpick } = $props();

  function facts(c) {
    const years = c.year ? `${c.year}${c.end_year && c.end_year !== c.year ? `–${c.end_year}` : ''}` : null;
    return [years, c.artist, c.network, c.country, c.uploader, (c.genres ?? []).slice(0, 4).join(', ') || null,
      c.runtime_minutes ? `${c.runtime_minutes} min` : null, c.duration_seconds ? fmtDuration(c.duration_seconds) : null,
      c.episodes ? `${c.episodes} episodes` : null, c.certificate].filter(Boolean).join(' · ');
  }
</script>

<ul class="found">
  {#each candidates as c, i (`${c.match?.source}:${c.match?.id}:${i}`)}
    {@const img = safeUrl(c.image, true)}
    {@const link = safeUrl(c.match?.url)}
    <li>
      {#if img}<img src={img} alt="" loading="lazy" referrerpolicy="no-referrer" />{:else}<span class="noimg" aria-hidden="true"></span>{/if}
      <div class="body">
        <b>{c.title}</b>
        <div class="tiny muted">{facts(c)}</div>
        {#if c.summary}<p class="small summary">{c.summary}</p>{/if}
        <div class="tiny muted">{c.match?.source}{#if link} · <a href={link} target="_blank" rel="noopener noreferrer">details ↗</a>{/if}</div>
      </div>
      <button class="small primary" onclick={() => onpick(c)}>This one</button>
    </li>
  {:else}
    <li class="muted small">Nothing found. Check the spelling or the year, or add it without a match.</li>
  {/each}
</ul>

<style>
  .found { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .5rem; max-height: 50vh; overflow-y: auto; }
  .found li { display: grid; grid-template-columns: 48px 1fr auto; gap: .6rem; align-items: start; padding: .5rem; border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .found img, .noimg { width: 48px; height: 68px; object-fit: cover; border-radius: 3px; background: var(--bg-sunken); }
  .body { min-width: 0; }
  .summary { margin: .25rem 0; display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
</style>
