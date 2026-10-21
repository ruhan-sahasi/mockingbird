// The live interview's lifecycle, kept apart from React so that tests can drive it with fakes.
// Interview.tsx is only the view over it.
//
// In this order: the mic (so a refused mic never creates an interview), the speakers, the
// interview itself, then its socket. Mic frames go out from the moment the socket opens, silent
// ones included, and keep going after End, because the server ends the call once 2 s pass
// without one (relay.py's browser_timeout_s). Frames from before the socket opens are dropped,
// not saved up.
//
// The server's text frames and close codes are in server/tests/fixtures/wire_protocol.json, which
// the tests on both sides read.

import { ApiError, type LimitReason, type StartResult } from './api'
import { AudioBlockedError } from './audio/context'
import type { Mic } from './audio/mic'
import type { Player } from './audio/player'
import { parseServerMessage } from './protocol'
import { sendFrame, type FrameSocket, type SocketHandlers } from './socket'

export type Phase =
  | { name: 'mic' } // waiting for the visitor to allow the mic
  | { name: 'starting' } // creating the interview
  | { name: 'calling' } // the socket is open; the interviewer has not been heard yet
  | { name: 'live'; capS: number; startedAt: number }
  | { name: 'ending' } // End was pressed; waiting for the server to say the call ended
  | { name: 'failed'; message: string; reportId?: string }

/** What the session needs of the interview socket once it is open. */
export type SessionSocket = FrameSocket & { close(): void }

/** Everything with a side effect. The page passes the real ones, and tests pass fakes. */
export type SessionDeps = {
  startMic(onFrame: (pcm: Int16Array<ArrayBuffer>) => void): Promise<Mic>
  createPlayer(onSpeaking: (speaking: boolean) => void): Promise<Player>
  startInterview(): Promise<StartResult>
  openSocket(path: string, handlers: SocketHandlers): SessionSocket
  /** A millisecond clock, for when the call went live. */
  now(): number
  setTimeout(run: () => void, ms: number): void
}

/** What the session tells the page. None of these is called once the page has stopped it. */
export type SessionEvents = {
  onPhase(phase: Phase): void
  onSpeaking(speaking: boolean): void
  onLimit(reason: LimitReason): void
  onEnded(interviewId: string): void
}

export type Session = {
  /** The End button. Does nothing until the socket is open, or once the call is over. */
  end(): void
  /** The page has gone: close the socket, the mic and the speakers, and say nothing more. */
  stop(): void
}

// The close codes routes_ws.py uses.
export const TOKEN_REJECTED = 4401
export const CALL_NOT_STARTED = 1011
// After End, how long to wait for the server's "ended" before going to the report anyway.
export const END_WAIT_MS = 5000
const END = JSON.stringify({ type: 'end' })

/** Start the interview. It runs until the call ends, fails, or `stop()` is called. */
export function runInterview(deps: SessionDeps, events: SessionEvents): Session {
  let cancelled = false // the page has gone
  let finished = false // the call is over, one way or another
  let ending = false
  let mic: Mic | null = null
  let player: Player | null = null
  let socket: SessionSocket | null = null
  let endCall = () => {}

  const stopAll = () => {
    socket?.close()
    socket = null
    mic?.stop()
    mic = null
    player?.close()
    player = null
  }
  const fail = (message: string, reportId?: string) => {
    finished = true
    stopAll()
    if (!cancelled) events.onPhase({ name: 'failed', message, reportId })
  }
  const leaveFor = (go: () => void) => {
    finished = true
    stopAll()
    if (!cancelled) go()
  }
  // Reads `socket` as each frame comes, so frames go out from the moment it opens.
  const onFrame = (pcm: Int16Array<ArrayBuffer>) => sendFrame(socket, pcm)

  async function run() {
    try {
      mic = await deps.startMic(onFrame)
    } catch (error) {
      fail(micProblem(error))
      return
    }
    if (cancelled) return stopAll()
    events.onPhase({ name: 'starting' })

    try {
      player = await deps.createPlayer((on) => {
        if (!cancelled) events.onSpeaking(on)
      })
    } catch (error) {
      fail(error instanceof AudioBlockedError ? audioBlocked() : `${PLAYER_FAILED} ${DEMO_UNUSED}`)
      return
    }
    if (cancelled) return stopAll()

    let started
    try {
      started = await deps.startInterview()
    } catch (error) {
      fail(error instanceof ApiError ? START_FAILED : UNREACHABLE)
      return
    }
    if (cancelled) return stopAll()
    if ('error' in started) {
      const reason = started.error
      leaveFor(() => events.onLimit(reason))
      return
    }

    const { interview_id: id, ws_path: path, cap_s: capS } = started
    let live = false
    socket = deps.openSocket(path, {
      onMessage(data) {
        if (finished) return
        if (typeof data === 'string') {
          const message = parseServerMessage(data)
          if (message?.type === 'flush') player?.flush()
          else if (message?.type === 'ended') leaveFor(() => events.onEnded(id))
          return
        }
        // After End the interviewer has nothing left to say that needs hearing.
        if (ending) return
        player?.push(new Int16Array(data, 0, data.byteLength >> 1))
        // The server's cap timer starts as the call's audio starts flowing, so the clock does too.
        if (!live) {
          live = true
          events.onPhase({ name: 'live', capS, startedAt: deps.now() })
        }
      },
      onClose(code) {
        if (finished) return
        // After End, a close without "ended" still means the call is over.
        if (ending) leaveFor(() => events.onEnded(id))
        else if (code === TOKEN_REJECTED) fail(TOKEN_USED)
        else if (code === CALL_NOT_STARTED && !live) fail(CALL_FAILED)
        else if (live) fail(DROPPED, id)
        else fail(DROPPED_BEFORE_START)
      },
    })
    events.onPhase({ name: 'calling' })

    endCall = () => {
      if (finished || ending || socket?.readyState !== WebSocket.OPEN) return
      ending = true
      socket.send(END)
      player?.flush()
      events.onPhase({ name: 'ending' })
      deps.setTimeout(() => {
        if (!finished) leaveFor(() => events.onEnded(id))
      }, END_WAIT_MS)
    }
  }

  void run()
  return {
    end: () => endCall(),
    stop: () => {
      cancelled = true
      finished = true
      stopAll()
    },
  }
}

const DEMO_UNUSED = 'No interview was started, so your demo is still unused.'
const PLAYER_FAILED = 'The page could not open your speakers.'
export const START_FAILED =
  'Mockingbird could not start an interview just now. Try again in a minute.'
export const UNREACHABLE =
  'Mockingbird could not be reached. Check your connection, then start again.'
export const TOKEN_USED =
  "The server did not accept this interview's link. Start again from the first page in a minute."
export const CALL_FAILED =
  'The interviewer could not be reached, so the call never started and your demo is still unused. Try again in a minute.'
export const DROPPED =
  'The connection dropped, so the interview ended early. What you said before that is still graded.'
export const DROPPED_BEFORE_START =
  'The connection dropped before the interview started. Try again in a minute.'

function audioBlocked(): string {
  return `This browser did not let the page use audio. Reload the page and start again. ${DEMO_UNUSED}`
}

/** The message for a mic that would not start, by getUserMedia's error. */
export function micProblem(error: unknown): string {
  if (error instanceof AudioBlockedError) return audioBlocked()
  const name = error instanceof DOMException ? error.name : ''
  switch (name) {
    case 'NotAllowedError':
    case 'SecurityError':
      return `Microphone access is blocked for this page. Allow it in your browser's site settings, then start again. ${DEMO_UNUSED}`
    case 'NotFoundError':
    case 'OverconstrainedError':
      return `No microphone was found. Connect one, then start again. ${DEMO_UNUSED}`
    case 'NotReadableError':
      return `Another app is using your microphone. Close it, then start again. ${DEMO_UNUSED}`
    default:
      return `Your microphone could not be started. ${DEMO_UNUSED}`
  }
}
