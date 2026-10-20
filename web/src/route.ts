// Which page the location hash names. The app has three: #/ is the landing page, #/interview is
// the live interview and #/report/{id} is one interview's report. Anything else is the landing
// page, so a mistyped or stale link still lands somewhere useful.

export type Route = { page: 'landing' } | { page: 'interview' } | { page: 'report'; id: string }

// Interview ids are UUIDs. Letters, digits and dashes are all an id can hold.
const REPORT = /^#\/report\/([A-Za-z0-9-]+)$/

export function parseRoute(hash: string): Route {
  if (hash === '#/interview') return { page: 'interview' }
  const report = REPORT.exec(hash)
  if (report) return { page: 'report', id: report[1] }
  return { page: 'landing' }
}

export const LANDING_HASH = '#/'
export const INTERVIEW_HASH = '#/interview'

export function reportHash(id: string): string {
  return `#/report/${id}`
}

/** What the app shows: a page, or the landing page with a redirect to it. */
export type Screen =
  | { page: 'landing'; redirect: boolean }
  | { page: 'interview' }
  | { page: 'report'; id: string }

/**
 * What to show for `route`. The interview page opens only after a Start in this visit
 * (`started`), which is where consent is given, and never on a phone, which gets no Start button.
 * A reload, a typed #/interview or the Forward button lands on the first page instead, and the
 * address is put back to it, so the mic never turns on without a fresh Start.
 */
export function screenFor(route: Route, started: boolean, phone: boolean): Screen {
  if (route.page === 'report') return route
  if (route.page === 'interview') {
    return started && !phone ? { page: 'interview' } : { page: 'landing', redirect: true }
  }
  return { page: 'landing', redirect: false }
}

/** Whether a Start still holds after the route moves from `from` to `to`: leaving the
 * interview page uses it up, so coming back to it needs a new one. */
export function startHolds(from: Route['page'], to: Route['page']): boolean {
  return !(from === 'interview' && to !== 'interview')
}
