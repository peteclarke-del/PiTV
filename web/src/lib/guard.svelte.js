// Wrap an async action so a second call is ignored while the first is in flight (double clicks
// on Scan, Build, Run now). `run.busy` is reactive: bind it to the button's `disabled`.
export function guard(fn) {
  let busy = $state(false);
  const run = async (...args) => {
    if (busy) return undefined;
    busy = true;
    try { return await fn(...args); } finally { busy = false; }
  };
  Object.defineProperty(run, 'busy', { get: () => busy, enumerable: true });
  return run;
}
