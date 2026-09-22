<script>
  // What pitv_content found online for a title about to be added (contract section 8), so the
  // admin can pick the right one. Images and links come from outside sources: https images
  // only, no referrer, links open in a new tab without access back to this page.
  import { fmtCount, fmtDuration, safeUrl } from '../../lib/format.js';

  let { candidates = [], known = [], onpick } = $props();

  // A result already in the catalogue is greyed out, so the same programme is not added twice.
  // The same confirmed identity is certain; failing that, the same title (ignoring case,
  // punctuation and a leading article) with the same year, a year apart, or no year on one
  // side. Music and adverts also need the same artist where both name one.
  const plain = (s) => String(s ?? '').toLowerCase().replace(/^(the|a|an)\s+/, '').replace(/[^a-z0-9]+/g, '');
  function already(c) {
    return known.find((k) => {
      if (k.match && c.match?.id != null && k.match.source === c.match.source && k.match.id === String(c.match.id)) return true;
      if (plain(k.title) !== plain(c.title)) return false;
      if (k.artist && c.artist && plain(k.artist) !== plain(c.artist)) return false;
      return !k.year || !c.year || Math.abs(k.year - c.year) <= 1;
    });
  }
  const where = (k) => k.channel_number ? `on channel ${k.channel_number} ${k.channel_name ?? ''}`.trim()
    : k.source === 'wanted' ? 'on the wanted list' : 'in the library';

  function facts(c) {
    const years = c.year ? `${c.year}${c.end_year && c.end_year !== c.year ? `–${c.end_year}` : ''}` : null;
    return [years, c.artist, c.network, c.country, c.uploader, (c.genres ?? []).slice(0, 4).join(', ') || null,
      c.runtime_minutes ? `${c.runtime_minutes} min` : null, c.duration_seconds ? fmtDuration(c.duration_seconds) : null,
      c.episodes ? `${c.episodes} episodes` : null,
      // A creator's channel has no episode count worth the wait: an exact one means listing the
      // whole channel, twenty seconds a candidate, in a dialog somebody is standing in front of.
      // Subscribers come free with the same probe and tell two same-named creators apart as well.
      typeof c.subscribers === 'number' ? `${fmtCount(c.subscribers)} subscribers` : null,
      c.certificate].filter(Boolean).join(' · ');
  }
</script>

<ul class="found">
  {#each candidates as c, i (`${c.match?.source}:${c.match?.id}:${i}`)}
    {@const img = safeUrl(c.image, true)}
    {@const link = safeUrl(c.match?.url)}
    {@const have = already(c)}
    <li class:added={!!have}>
      {#if img}<img src={img} alt="" loading="lazy" referrerpolicy="no-referrer" />{:else}<span class="noimg" aria-hidden="true"></span>{/if}
      <div class="body">
        <b>{c.title}</b>
        <div class="tiny muted">{facts(c)}</div>
        {#if c.summary}<p class="small summary">{c.summary}</p>{/if}
        <div class="tiny muted">{c.match?.source}{#if link} · <a href={link} target="_blank" rel="noopener noreferrer">details ↗</a>{/if}</div>
      </div>
      {#if have}<span class="tiny muted have">Already in the catalogue<br />{where(have)}</span>
      {:else}<button class="small primary" onclick={() => onpick(c)}>This one</button>{/if}
    </li>
  {:else}
    <li class="muted small">Nothing found. Check the spelling or the year, or add it without a match.</li>
  {/each}
</ul>
{#if candidates.some((c) => c.match?.source === 'tmdb')}
  <!-- TMDb's terms ask for this credit wherever its data is shown. -->
  <p class="tiny muted">This product uses the TMDB API but is not endorsed or certified by TMDB.</p>
{/if}

<style>
  .found { list-style: none; margin: 0; padding: 0; display: flex; flex-direction: column; gap: .5rem; max-height: 50vh; overflow-y: auto; }
  .found li { display: grid; grid-template-columns: 48px 1fr auto; gap: .6rem; align-items: start; padding: .5rem; border: 1px solid var(--border); border-radius: var(--radius-sm); }
  .found img, .noimg { width: 48px; height: 68px; object-fit: cover; border-radius: 3px; background: var(--bg-sunken); }
  .body { min-width: 0; }
  .found li.added { opacity: .5; }
  .have { text-align: right; max-width: 9rem; }
  .summary { margin: .25rem 0; display: -webkit-box; -webkit-line-clamp: 3; line-clamp: 3; -webkit-box-orient: vertical; overflow: hidden; }
</style>
