import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, getReport, pollReport, startInterview, wsUrl, type Report } from './api'

const ID = '0b6f2c1e-4a7d-4e7b-9a51-3f2d8c6b1a90'

const STARTED = {
  interview_id: ID,
  ws_path: `/ws/interview/${ID}?token=abc_DEF-123`,
  cap_s: 300,
}

const GRADED: Report = {
  status: 'graded',
  questions: [
    {
      question_idx: 0,
      question_text: 'Tell me about a time you got feedback you did not agree with.',
      items: [
        {
          item: 'situation',
          score: 3,
          rationale: 'The project and the team are clear.',
          quotes: ['we were shipping the billing page'],
          dropped_quotes: [],
          status: 'scored',
        },
      ],
    },
  ],
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

let fetchMock: ReturnType<typeof vi.fn>

beforeEach(() => {
  fetchMock = vi.fn()
  vi.stubGlobal('fetch', fetchMock)
})

afterEach(() => {
  vi.useRealTimers()
  vi.unstubAllGlobals()
})

describe('startInterview', () => {
  it('posts to /api/interviews and returns the new interview on 201', async () => {
    fetchMock.mockResolvedValueOnce(json(201, STARTED))
    await expect(startInterview()).resolves.toEqual(STARTED)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/api/interviews')
    expect(init.method).toBe('POST')
  })

  it.each(['visitor_limit', 'daily_budget'])('returns the limit on a 429 with %s', async (error) => {
    fetchMock.mockResolvedValueOnce(json(429, { error }))
    await expect(startInterview()).resolves.toEqual({ error })
  })

  it.each([
    ['a 500', () => json(500, { detail: 'Internal Server Error' })],
    ['a 429 with a reason it does not know', () => json(429, { error: 'too_many' })],
    ['a 429 that is not JSON', () => new Response('slow down', { status: 429 })],
    ['a 201 without the socket path', () => json(201, { interview_id: ID, cap_s: 300 })],
    ['a 201 with a cap that is not a number', () => json(201, { ...STARTED, cap_s: '300' })],
  ])('throws an ApiError for %s', async (_name, response) => {
    fetchMock.mockResolvedValueOnce(response())
    await expect(startInterview()).rejects.toBeInstanceOf(ApiError)
  })

  it('lets a network failure through', async () => {
    fetchMock.mockRejectedValueOnce(new TypeError('Failed to fetch'))
    await expect(startInterview()).rejects.toBeInstanceOf(TypeError)
  })
})

describe('getReport', () => {
  it('returns the report on 200, whatever its status', async () => {
    fetchMock.mockResolvedValueOnce(json(200, GRADED))
    await expect(getReport(ID)).resolves.toEqual(GRADED)
    fetchMock.mockResolvedValueOnce(json(200, { status: 'no_answers', questions: [] }))
    await expect(getReport(ID)).resolves.toEqual({ status: 'no_answers', questions: [] })
  })

  it('asks for the report of the interview it is given', async () => {
    fetchMock.mockResolvedValueOnce(json(200, GRADED))
    await getReport(ID)
    expect(fetchMock.mock.calls[0][0]).toBe(`/api/interviews/${ID}/report`)
  })

  it('says grading on 202', async () => {
    fetchMock.mockResolvedValueOnce(json(202, { status: 'grading' }))
    await expect(getReport(ID)).resolves.toEqual({ status: 'grading' })
  })

  it('says not found on 404', async () => {
    fetchMock.mockResolvedValueOnce(json(404, { detail: 'Not Found' }))
    await expect(getReport(ID)).resolves.toEqual({ status: 'not_found' })
  })

  it('throws an ApiError for any other status', async () => {
    fetchMock.mockResolvedValueOnce(json(500, { detail: 'Internal Server Error' }))
    await expect(getReport(ID)).rejects.toBeInstanceOf(ApiError)
  })

  it('throws an ApiError for a 200 that is not a report', async () => {
    fetchMock.mockResolvedValueOnce(json(200, { status: 'exploded' }))
    await expect(getReport(ID)).rejects.toBeInstanceOf(ApiError)
  })
})

describe('pollReport', () => {
  beforeEach(() => {
    vi.useFakeTimers()
  })

  it('asks again every 2 s while the report is grading, then returns it', async () => {
    fetchMock
      .mockResolvedValueOnce(json(202, { status: 'grading' }))
      .mockResolvedValueOnce(json(202, { status: 'grading' }))
      .mockResolvedValueOnce(json(200, GRADED))
    const done = pollReport(ID, new AbortController().signal)

    await vi.advanceTimersByTimeAsync(0)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1999)
    expect(fetchMock).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(1)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(2000)
    expect(fetchMock).toHaveBeenCalledTimes(3)

    await expect(done).resolves.toEqual(GRADED)
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetchMock).toHaveBeenCalledTimes(3)
  })

  it('says each time the server answered that it is still grading', async () => {
    const onGrading = vi.fn()
    const onRetry = vi.fn()
    fetchMock
      .mockResolvedValueOnce(json(202, { status: 'grading' }))
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(json(202, { status: 'grading' }))
      .mockResolvedValueOnce(json(200, GRADED))
    const done = pollReport(ID, new AbortController().signal, { onGrading, onRetry })

    await vi.advanceTimersByTimeAsync(0)
    expect(onGrading).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2000)
    expect(onGrading).toHaveBeenCalledTimes(1)
    expect(onRetry).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(2000)
    expect(onGrading).toHaveBeenCalledTimes(2)
    await vi.advanceTimersByTimeAsync(2000)
    await expect(done).resolves.toEqual(GRADED)
    expect(onGrading).toHaveBeenCalledTimes(2)
  })

  it('stops at once on 404', async () => {
    fetchMock.mockResolvedValueOnce(json(404, { detail: 'Not Found' }))
    const done = pollReport(ID, new AbortController().signal)
    await expect(done).resolves.toEqual({ status: 'not_found' })
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetchMock).toHaveBeenCalledTimes(1)
  })

  it('keeps asking after a failed request, and says each time it failed', async () => {
    const onRetry = vi.fn()
    fetchMock
      .mockRejectedValueOnce(new TypeError('Failed to fetch'))
      .mockResolvedValueOnce(json(502, {}))
      .mockResolvedValueOnce(json(200, GRADED))
    const done = pollReport(ID, new AbortController().signal, { onRetry })

    await vi.advanceTimersByTimeAsync(4000)
    await expect(done).resolves.toEqual(GRADED)
    expect(onRetry).toHaveBeenCalledTimes(2)
    expect(onRetry.mock.calls[0][0]).toBeInstanceOf(TypeError)
    expect(onRetry.mock.calls[1][0]).toBeInstanceOf(ApiError)
  })

  it('stops asking once aborted', async () => {
    // A fresh Response each time, since a body can be read only once.
    fetchMock.mockImplementation(async () => json(202, { status: 'grading' }))
    const controller = new AbortController()
    const done = pollReport(ID, controller.signal)
    const settled = expect(done).rejects.toThrow()

    await vi.advanceTimersByTimeAsync(2000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
    controller.abort()
    await settled
    await vi.advanceTimersByTimeAsync(10_000)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })
})

describe('wsUrl', () => {
  it('uses ws on an http page and keeps the host and the token', () => {
    expect(wsUrl(STARTED.ws_path, { protocol: 'http:', host: 'localhost:5173' })).toBe(
      `ws://localhost:5173/ws/interview/${ID}?token=abc_DEF-123`,
    )
  })

  it('uses wss on an https page', () => {
    expect(wsUrl('/ws/interview/x?token=t', { protocol: 'https:', host: 'mock.example' })).toBe(
      'wss://mock.example/ws/interview/x?token=t',
    )
  })
})
