// The interview page's clock. It is only a display: the server ends the call at the cap whatever
// this shows.

/** Whole seconds as m:ss, for example 300 as "5:00". Below zero, and NaN, show as "0:00". */
export function formatCountdown(seconds: number): string {
  const s = Number.isFinite(seconds) && seconds > 0 ? Math.floor(seconds) : 0
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/**
 * Whole seconds left of a `capS`-second interview that started at `startedAtMs`, at `nowMs` (both
 * from the same millisecond clock). A second counts as gone only once all of it has passed, so the
 * clock shows the cap at the start and reaches 0 exactly when the cap does. It stays between 0 and
 * the cap, even for a `nowMs` read before the start.
 */
export function secondsLeft(capS: number, startedAtMs: number, nowMs: number): number {
  const left = Math.ceil(capS - (nowMs - startedAtMs) / 1000)
  return Math.min(capS, Math.max(0, left))
}
