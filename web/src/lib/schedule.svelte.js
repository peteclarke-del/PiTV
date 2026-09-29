// One broadcast day of the schedule at a time, shared by the guide and the schedule editor.
// GET /api/schedule/days is {today, days: [{day, n, s, e}], horizon_end}.
import { tick, untrack } from 'svelte';
import { get, tryApi } from './api.js';
import { changes, clock } from './stores.svelte.js';

/** Unix bounds for drawing one broadcast day: its first slot up to the next day's first slot
 *  (or 8 h past its last slot, so the overnight replay of the final day is visible). */
function dayBounds(days, day) {
  const i = days?.days.findIndex((d) => d.day === day) ?? -1;
  if (i < 0) return null;
  const d = days.days[i], next = days.days[i + 1];
  return { start: d.s, end: next ? next.s : d.e + 8 * 3600 };
}

const hasDay = (days, day) => !!days?.days.some((d) => d.day === day);

/** The day list, the chosen day and that day's slots, refetched whenever the schedule changes.
 *  Construct during component initialisation: it registers its own effects. */
export class ScheduleDay {
  days = $state(null);
  failed = $state(false);   // the day list could not be fetched (already toasted)
  day = $state('');
  data = $state(null);      // GET /api/schedule for the chosen day: {channels, slots}
  loading = $state(false);
  grid = $state(null);      // the EpgGrid instance, for scrolling
  /** The view is on today and stays on it: when the broadcast day turns over it moves to the
   *  new one, and the grid keeps the present in sight. Choosing another day ends it. */
  following = $state(true);
  bounds = $derived(dayBounds(this.days, this.day));
  channelById = $derived(new Map((this.data?.channels ?? []).map((c) => [c.id, c])));
  #seq = 0;
  #onpick;
  #scrolled = false;        // whether today has been opened on the current time yet
  #asked = 0;               // when the day list was last asked for, in clock time

  /** `ads()` and `bands()` independently expose continuity and band components; `onload` runs
   *  after every slot fetch; `onpick(day)` runs when the day changes through pick() or goNow(). */
  constructor({ day = '', ads = () => false, bands = () => false, onload, onpick } = {}) {
    this.day = day;
    this.#onpick = onpick;
    $effect(() => { changes.schedule; untrack(() => this.loadDays()); });
    // The day list says which day is today, and it was only asked for when the schedule
    // changed: a guide left open past the start of the broadcast day went on showing
    // yesterday, its now-line gone off the end. Once today's stretch is over the list is asked
    // for again, at most once a minute in case no later day has been built.
    $effect(() => {
      const now = clock.ts;
      untrack(() => {
        const today = dayBounds(this.days, this.days?.today);
        if (this.days && (!today || now >= today.end) && now - this.#asked >= 60) this.loadDays();
      });
    });
    $effect(() => {
      const bounds = this.bounds, withAds = ads(), withBandItems = bands();
      if (!bounds) return;
      untrack(async () => {
        if (!(await this.#loadSlots(bounds, withAds, withBandItems))) return;
        // Open on the current time once, the first time today is shown.
        if (!this.#scrolled && this.day === this.days?.today) { this.#scrolled = true; await tick(); this.scrollToNow(); }
        onload?.(this.data);
      });
    });
  }

  loadDays = async () => {
    const d = await tryApi(get('/api/schedule/days'));
    this.#asked = clock.ts;
    this.failed = !d;
    if (!d) return;
    const was = this.days?.today;
    this.days = d;
    if (!this.day) this.following = true;
    else if (was === undefined) this.following = this.day === d.today;
    // Keep the chosen day if it is still built, else today, else the first built day.
    if (!hasDay(d, this.day)) this.day = hasDay(d, d.today) ? d.today : (d.days[0]?.day ?? '');
    // The day has turned over under a view that was on today: go with it.
    if (this.following && was && d.today !== was && hasDay(d, d.today) && this.day !== d.today) {
      this.#scrolled = false;
      this.day = d.today;
      this.#onpick?.(d.today);
    }
  };

  /** Fetch the slots for `bounds`; false when a newer request has superseded this one. */
  async #loadSlots(bounds, ads, bands) {
    const seq = ++this.#seq;
    this.loading = true;
    const r = await tryApi(get('/api/schedule', {
      start: bounds.start, end: bounds.end, ads: ads ? 1 : 0, bands: bands ? 1 : 0,
    }));
    if (seq !== this.#seq) return false;
    this.data = r ?? this.data;
    this.loading = false;
    return true;
  }

  pick = (day) => {
    this.day = day;
    this.following = day === this.days?.today;
    this.#onpick?.(day);
  };

  scrollToNow = () => this.grid?.scrollTo(clock.ts, 80);

  /** Scroll to the current time, switching to today first when it is built (a schedule that only
   *  starts tomorrow has no "now" to show). */
  goNow = async () => {
    const today = hasDay(this.days, this.days?.today) ? this.days.today : null;
    if (today && this.day !== today) { this.pick(today); await tick(); }
    this.following = this.day === today;
    this.scrollToNow();
  };
}
