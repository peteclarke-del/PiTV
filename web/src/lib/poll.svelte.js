// Polling that stops itself when the component unmounts and pauses while the tab is hidden.
// Call during component initialisation.

/** Run `fn` every `ms` while the component is mounted, the tab is visible and `enabled()` (reactive) returns true.
 *  A hidden tab skips ticks and runs `fn` once as soon as it is shown again. */
export function poll(fn, ms, enabled = () => true) {
  $effect(() => {
    if (!enabled()) return;
    const visible = () => document.visibilityState !== 'hidden';
    const t = setInterval(() => { if (visible()) fn(); }, ms);
    const wake = () => { if (visible()) fn(); };
    document.addEventListener('visibilitychange', wake);
    return () => { clearInterval(t); document.removeEventListener('visibilitychange', wake); };
  });
}
