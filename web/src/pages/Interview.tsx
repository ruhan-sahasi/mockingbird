// The live interview. In this order: the mic (so a refused mic never creates an interview), the
// speakers, the interview itself, then its socket. Mic frames go out from the moment the socket
// opens, silent ones included, because the server ends the call once 2 s pass without one. Frames
// from before the socket opens are dropped, not saved up.

import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { ApiError, startInterview, wsUrl, type LimitReason } from '../api'
import { AudioBlockedError } from '../audio/context'
import { startMic, type Mic } from '../audio/mic'
import { createPlayer, type Player } from '../audio/player'
import { formatCountdown, secondsLeft } from '../countdown'
import { parseServerMessage } from '../protocol'
import { LANDING_HASH, reportHash } from '../route'

type Props = {
  onLimit(reason: LimitReason): void
  onEnded(interviewId: string): void
}

type Phase =
  | { name: 'mic' } // waiting for the visitor to allow the mic
  | { name: 'starting' } // creating the interview
  | { name: 'calling' } // the socket is open; the interviewer has not been heard yet
  | { name: 'live'; capS: number; startedAt: number }
  | { name: 'ending' } // End was pressed; waiting for the server to say the call ended
  | { name: 'failed'; message: string; reportId?: string }

// The close codes routes_ws.py uses.
const TOKEN_REJECTED = 4401
const CALL_NOT_STARTED = 1011
// After End, how long to wait for the server's "ended" before going to the report anyway.
const END_WAIT_MS = 5000
const END = JSON.stringify({ type: 'end' })

const DEMO_UNUSED = 'No interview was started, so your demo is still unused.'

export default function Interview({ onLimit, onEnded }: Props) {
  const [phase, setPhase] = useState<Phase>({ name: 'mic' })
  const [speaking, setSpeaking] = useState(false)
  const [now, setNow] = useState(() => performance.now())
  // Set by the effect below, which owns the socket.
  const endCall = useRef<() => void>(() => {})

  const limitReached = useEffectEvent(onLimit)
  const callEnded = useEffectEvent(onEnded)

  useEffect(() => {
    let cancelled = false // the page has gone
    let finished = false // the call is over, one way or another
    let ending = false
    let mic: Mic | null = null
    let player: Player | null = null
    let socket: WebSocket | null = null

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
      if (!cancelled) setPhase({ name: 'failed', message, reportId })
    }
    const leaveFor = (go: () => void) => {
      finished = true
      stopAll()
      if (!cancelled) go()
    }
    const sendFrame = (pcm: Int16Array<ArrayBuffer>) => {
      if (socket?.readyState === WebSocket.OPEN) socket.send(pcm)
    }

    async function run() {
      try {
        mic = await startMic(sendFrame)
      } catch (error) {
        fail(micProblem(error))
        return
      }
      if (cancelled) return stopAll()
      setPhase({ name: 'starting' })

      try {
        player = await createPlayer((on) => {
          if (!cancelled) setSpeaking(on)
        })
      } catch (error) {
        fail(error instanceof AudioBlockedError ? audioBlocked() : `${PLAYER_FAILED} ${DEMO_UNUSED}`)
        return
      }
      if (cancelled) return stopAll()

      let started
      try {
        started = await startInterview()
      } catch (error) {
        fail(error instanceof ApiError ? START_FAILED : UNREACHABLE)
        return
      }
      if (cancelled) return stopAll()
      if ('error' in started) {
        const reason = started.error
        leaveFor(() => limitReached(reason))
        return
      }

      const { interview_id: id, ws_path: path, cap_s: capS } = started
      let live = false
      socket = new WebSocket(wsUrl(path, window.location))
      socket.binaryType = 'arraybuffer'
      setPhase({ name: 'calling' })

      socket.onmessage = (event: MessageEvent<string | ArrayBuffer>) => {
        if (finished) return
        if (typeof event.data === 'string') {
          const message = parseServerMessage(event.data)
          if (message?.type === 'flush') player?.flush()
          else if (message?.type === 'ended') leaveFor(() => callEnded(id))
          return
        }
        // After End the interviewer has nothing left to say that needs hearing.
        if (ending) return
        player?.push(new Int16Array(event.data, 0, event.data.byteLength >> 1))
        // The server's cap timer starts as the call's audio starts flowing, so the clock does too.
        if (!live) {
          live = true
          setPhase({ name: 'live', capS, startedAt: performance.now() })
        }
      }
      socket.onclose = (event) => {
        if (finished) return
        // After End, a close without "ended" still means the call is over.
        if (ending) leaveFor(() => callEnded(id))
        else if (event.code === TOKEN_REJECTED) fail(TOKEN_USED)
        else if (event.code === CALL_NOT_STARTED && !live) fail(CALL_FAILED)
        else if (live) fail(DROPPED, id)
        else fail(DROPPED_BEFORE_START)
      }
      endCall.current = () => {
        if (finished || ending || socket?.readyState !== WebSocket.OPEN) return
        ending = true
        socket.send(END)
        player?.flush()
        setPhase({ name: 'ending' })
        setTimeout(() => {
          if (!finished) leaveFor(() => callEnded(id))
        }, END_WAIT_MS)
      }
    }

    void run()
    return () => {
      cancelled = true
      finished = true
      stopAll()
    }
  }, [])

  const isLive = phase.name === 'live'
  useEffect(() => {
    if (!isLive) return
    const timer = setInterval(() => setNow(performance.now()), 250)
    return () => clearInterval(timer)
  }, [isLive])

  return (
    <main className="page interview">
      <header className="masthead">
        {/* Not a link: leaving this page ends the call. */}
        <span className="wordmark">Mockingbird</span>
      </header>
      {phase.name === 'failed' ? (
        <section className="notice" role="alert">
          <p>{phase.message}</p>
          {phase.reportId !== undefined && (
            <p>
              <a href={reportHash(phase.reportId)}>See the report</a>
            </p>
          )}
          <p>
            <a href={LANDING_HASH}>Back to the first page</a>
          </p>
        </section>
      ) : (
        <section className="call">
          {phase.name === 'live' ? (
            <>
              <p className="presence" data-speaking={speaking}>
                <span className="presence-dot" aria-hidden="true" />
                {speaking ? 'The interviewer is speaking' : 'Listening'}
              </p>
              <p className="clock" role="timer" aria-live="off">
                <span className="clock-time">
                  {formatCountdown(secondsLeft(phase.capS, phase.startedAt, now))}
                </span>
                <span className="clock-label">left in this interview</span>
              </p>
            </>
          ) : (
            <p className="presence" data-waiting="true" role="status">
              <span className="presence-dot" aria-hidden="true" />
              {WAITING[phase.name]}
            </p>
          )}
          {(phase.name === 'calling' || phase.name === 'live') && (
            <button type="button" className="button button-quiet" onClick={() => endCall.current()}>
              End interview
            </button>
          )}
          {phase.name === 'live' && (
            <p className="note">You can talk over the interviewer. It stops and listens.</p>
          )}
        </section>
      )}
    </main>
  )
}

const WAITING = {
  mic: 'Allow the microphone when your browser asks.',
  starting: 'Starting the interview…',
  calling: 'Calling the interviewer…',
  ending: 'Ending the interview…',
}

const PLAYER_FAILED = 'The page could not open your speakers.'
const START_FAILED = 'Mockingbird could not start an interview just now. Try again in a minute.'
const UNREACHABLE = 'Mockingbird could not be reached. Check your connection, then start again.'
const TOKEN_USED =
  "The server did not accept this interview's link. Start again from the first page in a minute."
const CALL_FAILED =
  'The interviewer could not be reached, so the call never started and your demo is still unused. Try again in a minute.'
const DROPPED = 'The connection dropped, so the interview ended early. What you said before that is still graded.'
const DROPPED_BEFORE_START = 'The connection dropped before the interview started. Try again in a minute.'

function audioBlocked(): string {
  return `This browser did not let the page use audio. Reload the page and start again. ${DEMO_UNUSED}`
}

function micProblem(error: unknown): string {
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
