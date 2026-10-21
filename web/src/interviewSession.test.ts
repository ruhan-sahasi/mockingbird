// The live interview's lifecycle, driven with a fake mic, speakers, start request and socket.
// The server's frames and close codes come from server/tests/fixtures/wire_protocol.json, which
// the server's own tests check against what the relay and the socket route really send.

import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { ApiError, type LimitReason, type StartResult } from './api'
import { AudioBlockedError } from './audio/context'
import {
  CALL_FAILED,
  DROPPED,
  DROPPED_BEFORE_START,
  END_WAIT_MS,
  runInterview,
  START_FAILED,
  TOKEN_USED,
  UNREACHABLE,
  type Phase,
  type SessionDeps,
} from './interviewSession'
import type { SocketHandlers } from './socket'

const WIRE = JSON.parse(
  readFileSync(new URL('../../server/tests/fixtures/wire_protocol.json', import.meta.url), 'utf8'),
) as {
  server_to_browser: Record<'flush' | 'ping' | 'ended', object>
  browser_to_server: { end: object }
  close_codes: { token_rejected: number; call_not_started: number }
}

const ID = 'b7c1e1a4-1d2f-4f1e-9a51-3f2d8c6b1a90'
const STARTED: StartResult = {
  interview_id: ID,
  ws_path: `/ws/interview/${ID}?token=t`,
  cap_s: 300,
}
const DROPPED_NO_CODE = 1006 // what a browser reports for a connection lost without a close

class FakeSocket {
  readyState: WebSocket['readyState'] = WebSocket.CONNECTING
  readonly sent: unknown[] = []
  closed = 0
  readonly path: string
  private readonly handlers: SocketHandlers

  constructor(path: string, handlers: SocketHandlers) {
    this.path = path
    this.handlers = handlers
  }

  send(data: unknown): void {
    this.sent.push(data)
  }

  /** The page closing it. Fired at once, which is harder on the session than a real socket. */
  close(): void {
    this.closed++
    this.readyState = WebSocket.CLOSED
    this.handlers.onClose(1000)
  }

  open(): void {
    this.readyState = WebSocket.OPEN
  }

  text(frame: object): void {
    this.handlers.onMessage(JSON.stringify(frame))
  }

  audio(samples = 320): void {
    this.handlers.onMessage(new ArrayBuffer(samples * 2))
  }

  /** The server closing it, or the network dropping it. */
  closedBy(code: number): void {
    this.readyState = WebSocket.CLOSED
    this.handlers.onClose(code)
  }

  /** The text frames the page sent, as they were sent. */
  texts(): string[] {
    return this.sent.filter((s): s is string => typeof s === 'string')
  }
}

/** One interview page's worth of fakes, and what each was asked to do. */
function setup(overrides: Partial<SessionDeps> = {}, started: StartResult = STARTED) {
  const h = {
    calls: [] as string[],
    phases: [] as Phase[],
    speaking: [] as boolean[],
    limits: [] as LimitReason[],
    ended: [] as string[],
    timers: [] as { run: () => void; ms: number }[],
    onFrame: null as ((pcm: Int16Array<ArrayBuffer>) => void) | null,
    onSpeaking: null as ((on: boolean) => void) | null,
    micStopped: 0,
    pushed: [] as Int16Array[],
    flushed: 0,
    playerClosed: 0,
    socket: null as FakeSocket | null,
    phase(): Phase | undefined {
      return h.phases.at(-1)
    },
    frame(): Int16Array<ArrayBuffer> {
      return new Int16Array(320)
    },
  }
  const deps: SessionDeps = {
    startMic: async (onFrame) => {
      h.calls.push('startMic')
      h.onFrame = onFrame
      return { stop: () => void h.micStopped++ }
    },
    createPlayer: async (onSpeaking) => {
      h.calls.push('createPlayer')
      h.onSpeaking = onSpeaking
      return {
        push: (pcm) => void h.pushed.push(pcm),
        flush: () => void h.flushed++,
        close: () => void h.playerClosed++,
      }
    },
    startInterview: async () => {
      h.calls.push('startInterview')
      return started
    },
    openSocket: (path, handlers) => {
      h.calls.push('openSocket')
      h.socket = new FakeSocket(path, handlers)
      return h.socket
    },
    now: () => 4321,
    setTimeout: (run, ms) => void h.timers.push({ run, ms }),
    ...overrides,
  }
  const session = runInterview(deps, {
    onPhase: (phase) => void h.phases.push(phase),
    onSpeaking: (on) => void h.speaking.push(on),
    onLimit: (reason) => void h.limits.push(reason),
    onEnded: (id) => void h.ended.push(id),
  })
  return { h, session }
}

/** Let every pending step of the start run. */
function settle(): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, 0))
}

/** A session whose socket is open, as when the server has accepted the token. */
async function connected(overrides: Partial<SessionDeps> = {}) {
  const fixture = setup(overrides)
  await settle()
  fixture.h.socket!.open()
  return fixture
}

/** A session that has heard the interviewer, so the clock runs. */
async function live() {
  const fixture = await connected()
  fixture.h.socket!.audio()
  return fixture
}

function assertAllReleased(h: ReturnType<typeof setup>['h']): void {
  expect(h.micStopped).toBe(1)
  expect(h.playerClosed).toBe(1)
  expect(h.socket?.closed ?? 1).toBe(1)
}

describe('starting', () => {
  it('asks for the mic, then the speakers, then starts the interview, then opens its socket', async () => {
    const { h } = setup()
    await settle()

    expect(h.calls).toEqual(['startMic', 'createPlayer', 'startInterview', 'openSocket'])
    expect(h.socket!.path).toBe(STARTED.ws_path)
    expect(h.phases.map((p) => p.name)).toEqual(['starting', 'calling'])
  })

  it.each([
    ['NotAllowedError', /Microphone access is blocked/],
    ['NotFoundError', /No microphone was found/],
    ['NotReadableError', /Another app is using your microphone/],
  ])('never creates an interview when the mic fails with %s', async (name, message) => {
    const { h } = setup({
      startMic: async () => {
        throw new DOMException('', name)
      },
    })
    await settle()

    expect(h.calls).toEqual([])
    expect(h.phase()).toMatchObject({ name: 'failed', message })
    expect((h.phase() as { message: string }).message).toMatch(/your demo is still unused/)
  })

  it('never creates an interview when the speakers cannot open, and frees the mic', async () => {
    const { h } = setup({
      createPlayer: async () => {
        throw new AudioBlockedError()
      },
    })
    await settle()

    expect(h.calls).toEqual(['startMic'])
    expect(h.micStopped).toBe(1)
    expect(h.phase()).toMatchObject({ name: 'failed' })
    expect((h.phase() as { message: string }).message).toMatch(/did not let the page use audio/)
  })

  it.each([
    ['the server refuses oddly', new ApiError(500, 'unexpected'), START_FAILED],
    ['the server cannot be reached', new TypeError('Failed to fetch'), UNREACHABLE],
  ])('says so when %s, and lets go of the mic and the speakers', async (_case, error, message) => {
    const { h } = setup({
      startInterview: async () => {
        throw error
      },
    })
    await settle()

    expect(h.phase()).toEqual({ name: 'failed', message, reportId: undefined })
    expect(h.calls).not.toContain('openSocket')
    assertAllReleased(h)
  })

  it.each(['visitor_limit', 'daily_budget'] as const)(
    'goes back with the %s limit and opens no socket',
    async (reason) => {
      const { h } = setup({}, { error: reason })
      await settle()

      expect(h.limits).toEqual([reason])
      expect(h.calls).not.toContain('openSocket')
      expect(h.phases.map((p) => p.name)).toEqual(['starting'])
      assertAllReleased(h)
    },
  )
})

describe('mic frames', () => {
  it('are dropped before the socket opens, and sent from the moment it does', async () => {
    const { h } = setup()
    await settle()
    h.onFrame!(h.frame()) // the socket is still connecting
    expect(h.socket!.sent).toEqual([])

    h.socket!.open()
    const frame = h.frame()
    h.onFrame!(frame)
    expect(h.socket!.sent).toEqual([frame])
  })

  it('keep going after End, so the server does not take silence for a lost browser', async () => {
    const { h, session } = await live()
    session.end()
    const after = h.frame()
    h.onFrame!(after)
    expect(h.socket!.sent.at(-1)).toBe(after)
  })
})

describe('the server', () => {
  it('starts the clock with the first audio, and plays the audio', async () => {
    const { h } = await connected()
    expect(h.phase()).toEqual({ name: 'calling' })

    h.socket!.audio(320)
    h.socket!.audio(320)

    expect(h.phase()).toEqual({ name: 'live', capS: 300, startedAt: 4321 })
    expect(h.phases.filter((p) => p.name === 'live')).toHaveLength(1)
    expect(h.pushed.map((pcm) => pcm.length)).toEqual([320, 320])
  })

  it('flushes the speakers on its flush frame, and nothing else', async () => {
    const { h } = await live()

    h.socket!.text(WIRE.server_to_browser.flush)
    h.socket!.text(WIRE.server_to_browser.ping)

    expect(h.flushed).toBe(1)
    expect(h.phase()).toMatchObject({ name: 'live' })
    expect(h.ended).toEqual([])
  })

  it('sends the page to the report on its ended frame, and lets go of everything', async () => {
    const { h } = await live()

    h.socket!.text(WIRE.server_to_browser.ended)

    expect(h.ended).toEqual([ID])
    assertAllReleased(h)
    h.socket!.audio() // anything after that is ignored
    expect(h.pushed).toHaveLength(1)
  })

  it('passes on when the interviewer starts and stops speaking', async () => {
    const { h } = await live()
    h.onSpeaking!(true)
    h.onSpeaking!(false)
    expect(h.speaking).toEqual([true, false])
  })
})

describe('the End button', () => {
  it('sends one end frame, flushes the speakers and waits for the server', async () => {
    const { h, session } = await live()

    session.end()
    session.end() // a second press does nothing more

    expect(h.socket!.texts().map((t) => JSON.parse(t))).toEqual([WIRE.browser_to_server.end])
    expect(h.flushed).toBe(1)
    expect(h.phase()).toEqual({ name: 'ending' })
    h.socket!.audio() // the interviewer has nothing left that needs hearing
    expect(h.pushed).toHaveLength(1)
  })

  it('goes to the report when the server says the call ended', async () => {
    const { h, session } = await live()
    session.end()

    h.socket!.text(WIRE.server_to_browser.ended)
    h.timers[0].run() // the fallback, later, does nothing

    expect(h.ended).toEqual([ID])
  })

  it('goes to the report when the socket closes without an ended frame', async () => {
    const { h, session } = await live()
    session.end()

    h.socket!.closedBy(DROPPED_NO_CODE)

    expect(h.ended).toEqual([ID])
    expect(h.phases.some((p) => p.name === 'failed')).toBe(false)
  })

  it(`goes to the report after ${END_WAIT_MS} ms when the server says nothing`, async () => {
    const { h, session } = await live()
    session.end()
    expect(h.ended).toEqual([])

    expect(h.timers.map((t) => t.ms)).toEqual([END_WAIT_MS])
    h.timers[0].run()

    expect(h.ended).toEqual([ID])
    assertAllReleased(h)
  })

  it('works before the interviewer has been heard', async () => {
    const { h, session } = await connected()
    session.end()
    expect(h.socket!.texts()).toHaveLength(1)
    expect(h.phase()).toEqual({ name: 'ending' })
  })
})

describe('a socket that closes on its own', () => {
  it('with the token-rejected code says the link was not accepted', async () => {
    const { h } = await connected()
    h.socket!.closedBy(WIRE.close_codes.token_rejected)
    expect(h.phase()).toEqual({ name: 'failed', message: TOKEN_USED, reportId: undefined })
    assertAllReleased(h)
  })

  it('with the call-not-started code before any audio says the demo is unused', async () => {
    const { h } = await connected()
    h.socket!.closedBy(WIRE.close_codes.call_not_started)
    expect(h.phase()).toEqual({ name: 'failed', message: CALL_FAILED, reportId: undefined })
  })

  it.each([
    ['the call-not-started code', WIRE.close_codes.call_not_started],
    ['a dropped connection', DROPPED_NO_CODE],
  ])('with %s after the call went live links to the report', async (_case, code) => {
    const { h } = await live()
    h.socket!.closedBy(code)
    expect(h.phase()).toEqual({ name: 'failed', message: DROPPED, reportId: ID })
    assertAllReleased(h)
  })

  it('before the call went live says to try again', async () => {
    const { h } = await connected()
    h.socket!.closedBy(DROPPED_NO_CODE)
    expect(h.phase()).toEqual({
      name: 'failed',
      message: DROPPED_BEFORE_START,
      reportId: undefined,
    })
  })
})

describe('leaving the page', () => {
  it('while the mic is being asked for frees it once it comes, and starts nothing', async () => {
    let grant!: () => void
    let stopped = 0
    const { h, session } = setup({
      startMic: () =>
        new Promise((resolve) => {
          grant = () => resolve({ stop: () => void stopped++ })
        }),
    })
    session.stop()
    grant()
    await settle()

    expect(stopped).toBe(1)
    expect(h.calls).toEqual([])
    expect(h.phases).toEqual([])
  })

  it('during the call closes the socket, the mic and the speakers, and says no more', async () => {
    const { h, session } = await live()
    const before = h.phases.length

    session.stop()
    h.socket!.text(WIRE.server_to_browser.ended)
    h.onSpeaking!(true)

    assertAllReleased(h)
    expect(h.phases).toHaveLength(before)
    expect(h.ended).toEqual([])
    expect(h.speaking).toEqual([])
  })
})
