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
