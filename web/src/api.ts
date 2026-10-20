// The browser's HTTP calls to the server (server/mockingbird/routes_api.py), and where the
// interview socket lives. The pages are served from the same origin as the API, so every path is
// relative, and the visitor cookie the start sets goes along on its own.

export type StartedInterview = {
  interview_id: string
  // The socket path, carrying a one-time token: /ws/interview/{id}?token=...
  ws_path: string
  // The longest the call may run, in seconds.
  cap_s: number
}

// Why a start was refused: this visitor has had today's demo, or the day's demo minutes are gone.
export type LimitReason = 'visitor_limit' | 'daily_budget'

export type StartResult = StartedInterview | { error: LimitReason }

// One rubric item of one question, as report.py's verify_quotes makes it. `quotes` are the ones
// found in what the candidate said. `dropped_quotes` are the grader's quotes that were not, and
// are never shown. An item with no kept quote has no score.
export type ItemReport = {
  item: string
  score: number | null
  rationale: string
  quotes: string[]
  dropped_quotes: string[]
  status: 'scored' | 'not_enough_evidence'
}

export type QuestionReport = {
  question_idx: number
  question_text: string
  items: ItemReport[]
}

// "no_answers": the candidate said nothing, so nothing was graded. "grading_failed": Claude
// refused or errored. Both come with no questions.
export type Report =
  | { status: 'graded'; questions: QuestionReport[] }
  | { status: 'no_answers' | 'grading_failed'; questions: QuestionReport[] }

// What asking for a report can give: the report, "grading" while it is on its way, or
// "not_found" for an interview that does not exist, never ran a call, or is still live.
export type ReportResult = Report | { status: 'grading' } | { status: 'not_found' }

/** The server answered with a status or body this client does not expect. */
export class ApiError extends Error {
  readonly status: number

  constructor(status: number, message: string) {
    super(message)
    this.name = 'ApiError'
    this.status = status
  }
}

/**
 * Create an interview. Gives its id, socket path and cap on 201, or the limit that refused it on
 * 429. Throws ApiError for anything else, and lets a network failure through as it is.
 */
export async function startInterview(): Promise<StartResult> {
  const res = await fetch('/api/interviews', { method: 'POST' })
  const body = await readJson(res)
  if (res.status === 201 && isStarted(body)) {
    return { interview_id: body.interview_id, ws_path: body.ws_path, cap_s: body.cap_s }
  }
  if (res.status === 429 && isRecord(body) && isLimit(body.error)) {
    return { error: body.error }
  }
  throw new ApiError(res.status, `unexpected answer to starting an interview: ${res.status}`)
}

/**
 * Ask once for an interview's report: the report on 200, "grading" on 202, "not_found" on 404.
 * Throws ApiError for anything else.
 */
export async function getReport(id: string, signal?: AbortSignal): Promise<ReportResult> {
  const res = await fetch(`/api/interviews/${encodeURIComponent(id)}/report`, { signal })
  if (res.status === 202) return { status: 'grading' }
  if (res.status === 404) return { status: 'not_found' }
  const body = await readJson(res)
  if (res.status === 200 && isReport(body)) return body
  throw new ApiError(res.status, `unexpected answer to asking for a report: ${res.status}`)
}

export type PollOptions = {
  /** Called each time the server answers that the report is still being graded. */
  onGrading?: () => void
  /** Called with the error each time a request fails. */
  onRetry?: (error: unknown) => void
  everyMs?: number
}

/**
 * Ask for the report every `everyMs` (2 s by default) until it is no longer grading, and give
 * what came back. A request that fails is asked again on the same schedule, since the report is
 * still on its way. Rejects once `signal` aborts.
 */
export async function pollReport(
  id: string,
  signal: AbortSignal,
  { onGrading, onRetry, everyMs = 2000 }: PollOptions = {},
): Promise<Report | { status: 'not_found' }> {
  for (;;) {
    try {
      const result = await getReport(id, signal)
      if (result.status !== 'grading') return result
      onGrading?.()
    } catch (error) {
      if (signal.aborted) throw signal.reason
      onRetry?.(error)
    }
    await sleep(everyMs, signal)
  }
}

/** The socket URL for a path from startInterview, on the host that served this page. */
export function wsUrl(path: string, page: { protocol: string; host: string }): string {
  return `${page.protocol === 'https:' ? 'wss:' : 'ws:'}//${page.host}${path}`
}

async function readJson(res: Response): Promise<unknown> {
  try {
    return await res.json()
  } catch {
    return null
  }
}

function isRecord(x: unknown): x is Record<string, unknown> {
  return typeof x === 'object' && x !== null && !Array.isArray(x)
}

function isLimit(x: unknown): x is LimitReason {
  return x === 'visitor_limit' || x === 'daily_budget'
}

function isStarted(x: unknown): x is StartedInterview {
  return (
    isRecord(x) &&
    typeof x.interview_id === 'string' &&
    typeof x.ws_path === 'string' &&
    typeof x.cap_s === 'number'
  )
}

// The status is what the page branches on, so it is checked. The rest is the server's own
// report and is trusted as it is.
function isReport(x: unknown): x is Report {
  return (
    isRecord(x) &&
    (x.status === 'graded' || x.status === 'no_answers' || x.status === 'grading_failed') &&
    Array.isArray(x.questions)
  )
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve, reject) => {
    if (signal.aborted) {
      reject(signal.reason)
      return
    }
    const timer = setTimeout(() => {
      signal.removeEventListener('abort', onAbort)
      resolve()
    }, ms)
    function onAbort() {
      clearTimeout(timer)
      reject(signal.reason)
    }
    signal.addEventListener('abort', onAbort, { once: true })
  })
}
