// The first page: what Mockingbird is, the headphones note, consent, and Start. A phone gets the
// laptop message instead, and a visitor the server turned away gets the limit message.

import { useState } from 'react'
import type { LimitReason } from '../api'

type Props = {
  phone: boolean
  limit: LimitReason | null
  onStart(): void
}

const CONSENT =
  'Your voice is sent to Vapi and Claude to run and grade this interview. Nothing is shared publicly.'
const PHONE = 'Mockingbird needs a laptop or desktop browser with a mic. Open this link there.'
const LIMITS: Record<LimitReason, string> = {
  visitor_limit: "You've used today's demo. Come back tomorrow.",
  daily_budget: 'The demo is resting for today. Come back tomorrow.',
}

export default function Landing({ phone, limit, onStart }: Props) {
  const [consent, setConsent] = useState(false)

  return (
    <main className="page landing">
      <h1 className="hero">Mockingbird</h1>
      <p className="lede">
        A practice behavioral interview you answer out loud: an interviewer asks about your past
        work and follows up on what you say, then a grader scores your answer with quotes from it.
      </p>
      <p className="headphones">
        Use headphones. Through speakers, the interviewer hears itself and cuts its own lines short.
      </p>

      {phone ? (
        <p className="notice">{PHONE}</p>
      ) : limit !== null ? (
        <p className="notice" role="status">
          {LIMITS[limit]}
        </p>
      ) : (
        <form
          className="start"
          onSubmit={(event) => {
            event.preventDefault()
            if (consent) onStart()
          }}
        >
          <label className="consent">
            <input
              type="checkbox"
              checked={consent}
              onChange={(event) => setConsent(event.target.checked)}
            />
            <span>{CONSENT}</span>
          </label>
          <button type="submit" className="button" disabled={!consent}>
            Start the interview
          </button>
          <p className="hint">Your browser will ask to use your microphone.</p>
        </form>
      )}
    </main>
  )
}
