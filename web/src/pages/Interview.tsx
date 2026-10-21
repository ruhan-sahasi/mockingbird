// The live interview page. What happens when (the mic first, then the speakers, the interview and
// its socket; the End button; how the call can end) is in interviewSession.ts. This is the view.

import { useEffect, useEffectEvent, useRef, useState } from 'react'
import { startInterview, wsUrl, type LimitReason } from '../api'
import { startMic } from '../audio/mic'
import { createPlayer } from '../audio/player'
import { formatCountdown, secondsLeft } from '../countdown'
import { runInterview, type Phase, type Session } from '../interviewSession'
import { LANDING_HASH, reportHash } from '../route'
import { openSocket } from '../socket'

type Props = {
  onLimit(reason: LimitReason): void
  onEnded(interviewId: string): void
}

export default function Interview({ onLimit, onEnded }: Props) {
  const [phase, setPhase] = useState<Phase>({ name: 'mic' })
  const [speaking, setSpeaking] = useState(false)
  const [now, setNow] = useState(() => performance.now())
  const session = useRef<Session | null>(null)

  const limitReached = useEffectEvent(onLimit)
  const callEnded = useEffectEvent(onEnded)

  useEffect(() => {
    const running = runInterview(
      {
        startMic,
        createPlayer,
        startInterview,
        openSocket: (path, handlers) => openSocket(wsUrl(path, window.location), handlers),
        now: () => performance.now(),
        setTimeout: (run, ms) => {
          window.setTimeout(run, ms)
        },
      },
      {
        onPhase: setPhase,
        onSpeaking: setSpeaking,
        onLimit: (reason) => limitReached(reason),
        onEnded: (id) => callEnded(id),
      },
    )
    session.current = running
    return () => running.stop()
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
            <button
              type="button"
              className="button button-quiet"
              onClick={() => session.current?.end()}
            >
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
