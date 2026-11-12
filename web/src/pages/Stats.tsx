// The latency stats over every call in the last 7 days, from GET /api/stats. The page asks once
// and shows the numbers in plain tables: the two the browser measures, then the median of each
// server hop, the cut-in rate, and one sentence when interrupt to silence is slow enough that
// the interviewer audio should be ducked in the browser.
//
// The numbers are about all the calls together, so the page needs no Start and names no one.

import { useEffect, useState } from 'react'
import { getStats, type Spread, type Stats as StatsData } from '../api'
import { LANDING_HASH } from '../route'

const DAYS = 7

export default function Stats() {
  // null until the server answers.
  const [stats, setStats] = useState<StatsData | null>(null)
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    getStats(DAYS, controller.signal).then(setStats, () => {
      if (!controller.signal.aborted) setFailed(true)
    })
    return () => controller.abort()
  }, [])

  return (
    <main className="page stats">
      <header className="masthead">
        <a className="wordmark" href={LANDING_HASH}>
          Mockingbird
        </a>
      </header>
      <StatsView stats={stats} failed={failed} />
    </main>
  )
}

const HOPS: [keyof StatsData['hops_ms'], string][] = [
  ['request_to_first_token', 'Brain request to first token from Claude'],
  ['first_token_to_first_chunk', 'First token to first chunk sent to Vapi'],
  ['first_chunk_to_assistant_started', 'First chunk to Vapi starting to speak'],
  ['assistant_started_to_first_audio', 'Vapi starting to speak to first audible frame'],
]

/**
 * The stats, or what to say while there are none. `stats` is null before the server has
 * answered, and `failed` says the request failed.
 */
export function StatsView({
  stats,
  failed = false,
}: {
  stats: StatsData | null
  failed?: boolean
}) {
  return (
    <>
      <h1 className="report-title">Latency</h1>
      {stats === null ? (
        failed && (
          <p className="notice" role="status">
            The server did not answer. Reload the page to try again.
          </p>
        )
      ) : stats.turns === 0 ? (
        <p className="notice" role="status">
          No measured turns yet.
        </p>
      ) : (
        <>
          <p className="lede">{traced(stats.turns)}</p>
          <table className="stats-table">
            <caption>Measured in the browser</caption>
            <thead>
              <tr>
                <th scope="col">Measure</th>
                <th scope="col">Median</th>
                <th scope="col">95th percentile</th>
                <th scope="col">Turns measured</th>
              </tr>
            </thead>
            <tbody>
              <SpreadRow
                label="End of speech to interviewer audible"
                spread={stats.stop_to_audible_ms}
              />
              <SpreadRow label="Interrupt to silence" spread={stats.interrupt_to_silence_ms} />
            </tbody>
          </table>
          <table className="stats-table">
            <caption>Server hops</caption>
            <thead>
              <tr>
                <th scope="col">Hop</th>
                <th scope="col">Median</th>
              </tr>
            </thead>
            <tbody>
              {HOPS.map(([hop, label]) => (
                <tr key={hop}>
                  <th scope="row">{label}</th>
                  <td>{ms(stats.hops_ms[hop])}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="stats-note">
            Cut-in rate: {percent(stats.cut_in_rate)}. A cut-in is the candidate talking over a
            reply within 1.5 s of its start.
          </p>
          {stats.consider_ducking && (
            <p className="stats-note">
              Interrupt to silence p95 is above 300 ms, so consider ducking the interviewer audio in
              the browser as soon as the candidate starts talking.
            </p>
          )}
        </>
      )}
    </>
  )
}

function SpreadRow({ label, spread }: { label: string; spread: Spread }) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>{ms(spread.p50)}</td>
      <td>{ms(spread.p95)}</td>
      <td>{spread.n}</td>
    </tr>
  )
}

function traced(turns: number): string {
  const replies = turns === 1 ? 'reply' : 'replies'
  return `${turns} interviewer ${replies} traced in the last ${DAYS} days.`
}

function ms(value: number | null): string {
  return value === null ? 'n/a' : `${Math.round(value)} ms`
}

function percent(share: number | null): string {
  return share === null ? 'n/a' : `${Math.round(share * 100)}%`
}
