// Polling that stops itself when the component unmounts. Call during component initialisation.

/** Run `fn` every `ms` while the component is mounted and `enabled()` (reactive) returns true. */
export function poll(fn, ms, enabled = () => true) {
  $effect(() => {
    if (!enabled()) return;
    const t = setInterval(fn, ms);
    return () => clearInterval(t);
  });
}
