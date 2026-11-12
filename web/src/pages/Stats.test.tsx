import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { Stats } from '../api'
import { StatsView } from './Stats'

// What GET /api/stats answers (server/mockingbird/stats.py), with numbers that need rounding.
const STATS: Stats = {
  turns: 4,
  stop_to_audible_ms: { p50: 1420.4, p95: 2100, n: 3 },
  hops_ms: {
    request_to_first_token: 410.2,
    first_token_to_first_chunk: 2,
    first_chunk_to_assistant_started: 380,
    assistant_started_to_first_audio: 95.6,
  },
  cut_in_rate: 0.25,
  interrupt_to_silence_ms: { p50: 180, p95: 250, n: 2 },
  consider_ducking: false,
}

const EMPTY: Stats = {
  turns: 0,
  stop_to_audible_ms: { p50: null, p95: null, n: 0 },
  hops_ms: {
    request_to_first_token: null,
    first_token_to_first_chunk: null,
    first_chunk_to_assistant_started: null,
    assistant_started_to_first_audio: null,
  },
  cut_in_rate: null,
  interrupt_to_silence_ms: { p50: null, p95: null, n: 0 },
  consider_ducking: false,
}

function plain(html: string): string {
  return html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;/g, "'")
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim()
}

// The text a reader sees, and each table row as its cells joined by " | ".
type Rendered = { html: string; text: string; rows: string[] }

function render(stats: Stats | null, failed = false): Rendered {
  const html = renderToStaticMarkup(<StatsView stats={stats} failed={failed} />)
  const rows = [...html.matchAll(/<tr>(.*?)<\/tr>/g)].map((row) =>
    [...row[1].matchAll(/<t[hd][^>]*>(.*?)<\/t[hd]>/g)].map((cell) => plain(cell[1])).join(' | '),
  )
  return { html, text: plain(html), rows }
}

describe('StatsView', () => {
  it('shows the browser numbers in a table, rounded to whole ms', () => {
    const { rows } = render(STATS)
    expect(rows).toContain('Measure | Median | 95th percentile | Turns measured')
    expect(rows).toContain('End of speech to interviewer audible | 1420 ms | 2100 ms | 3')
    expect(rows).toContain('Interrupt to silence | 180 ms | 250 ms | 2')
  })

  it('shows the median of each server hop', () => {
    const { rows } = render(STATS)
    expect(rows).toContain('Brain request to first token from Claude | 410 ms')
    expect(rows).toContain('First token to first chunk sent to Vapi | 2 ms')
    expect(rows).toContain('First chunk to Vapi starting to speak | 380 ms')
    expect(rows).toContain('Vapi starting to speak to first audible frame | 96 ms')
  })

  it('shows how many replies were traced and the cut-in rate', () => {
    const { text } = render(STATS)
    expect(text).toContain('4 interviewer replies traced in the last 7 days.')
    expect(text).toContain('Cut-in rate: 25%')
  })

  it('shows n/a for a number with nothing measured, never 0 ms', () => {
    const stats: Stats = {
      ...STATS,
      interrupt_to_silence_ms: { p50: null, p95: null, n: 0 },
      hops_ms: { ...STATS.hops_ms, assistant_started_to_first_audio: null },
      cut_in_rate: null,
    }
    const { rows, text } = render(stats)
    expect(rows).toContain('Interrupt to silence | n/a | n/a | 0')
    expect(rows).toContain('Vapi starting to speak to first audible frame | n/a')
    expect(text).toContain('Cut-in rate: n/a')
  })

  it('says in one sentence to consider ducking when interrupt to silence p95 > 300 ms', () => {
    const { text } = render({ ...STATS, consider_ducking: true })
    expect(text).toContain(
      'Interrupt to silence p95 is above 300 ms, so consider ducking the interviewer audio in ' +
        'the browser as soon as the candidate starts talking.',
    )
  })

  it('says nothing about ducking when it is not needed', () => {
    expect(render(STATS).text).not.toMatch(/ducking/i)
  })

  it('says there are no measured turns yet, with no table, when nothing was traced', () => {
    const { html, text } = render(EMPTY)
    expect(text).toContain('No measured turns yet.')
    expect(html).not.toContain('<table')
  })

  it('says the numbers could not be loaded when the request failed', () => {
    const { text, html } = render(null, true)
    expect(text).toContain('The server did not answer. Reload the page to try again.')
    expect(html).not.toContain('<table')
  })

  it('shows only the title before the server has answered', () => {
    expect(render(null).text).toBe('Latency')
  })

  it('has no apostrophes and no em or en dashes in its copy', () => {
    for (const { html } of [
      render({ ...STATS, consider_ducking: true, cut_in_rate: null }),
      render(EMPTY),
      render(null, true),
    ]) {
      expect(html).not.toMatch(/['’–—]|&#x27;/)
    }
  })
})
