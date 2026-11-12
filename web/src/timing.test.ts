// The browser's two latency numbers, on a fake clock: end of speech to the interviewer being
// heard, and the candidate cutting in to the interviewer going quiet.

import { describe, expect, it } from 'vitest'
import { TurnTimer } from './timing'

/** A timer on a clock the test sets, with `latencyMs` of output latency. */
function setup(latencyMs = 20) {
  const clock = { now: 0 }
  const timer = new TurnTimer({ now: () => clock.now, outputLatencyMs: () => latencyMs })
  const at = (ms: number) => {
    clock.now = ms
    return timer
  }
  return { timer, at }
}

describe('stop to audible', () => {
  it('runs from the backdated stop to the first sample leaving the speakers', () => {
    const { at } = setup(20)
    at(500).vad({ speaking: true })
    at(1000).vad({ speaking: false, quietMs: 300 })
    expect(at(2100).interviewerAudible(true)).toEqual({ stop_to_audible_ms: 1420 })
  })

  it('gives nothing when the interviewer is heard with no stop pending', () => {
    const { at } = setup()
    expect(at(2100).interviewerAudible(true)).toBeNull()
  })

  it('is given once per stop: the next time the interviewer is heard gives nothing', () => {
    const { at } = setup()
    at(1000).vad({ speaking: false, quietMs: 300 })
    expect(at(2100).interviewerAudible(true)).not.toBeNull()
    expect(at(2500).interviewerAudible(false)).toBeNull()
    expect(at(2900).interviewerAudible(true)).toBeNull()
  })

  it('runs from the latest stop', () => {
    const { at } = setup(0)
    at(1000).vad({ speaking: false, quietMs: 300 })
    at(1400).vad({ speaking: true })
    at(2000).vad({ speaking: false, quietMs: 300 })
    expect(at(2500).interviewerAudible(true)).toEqual({ stop_to_audible_ms: 800 })
  })

  it('reads the output latency when the interviewer is heard', () => {
    const clock = { now: 0 }
    let latency = 0
    const timer = new TurnTimer({ now: () => clock.now, outputLatencyMs: () => latency })
    clock.now = 1000
    timer.vad({ speaking: false, quietMs: 300 })
    latency = 45
    clock.now = 1500
    expect(timer.interviewerAudible(true)).toEqual({ stop_to_audible_ms: 845 })
  })
})

describe('interrupt to silence', () => {
  it('runs from the candidate cutting in to the interviewer going quiet after the flush', () => {
    const { at } = setup(20)
    at(4000).interviewerAudible(true)
    at(5000).vad({ speaking: true })
    at(5100).flushed()
    expect(at(5180).interviewerAudible(false)).toEqual({ interrupt_to_silence_ms: 180 })
  })

  it('gives nothing when the interviewer stops on its own, with no flush', () => {
    const { at } = setup()
    at(4000).interviewerAudible(true)
    at(5000).vad({ speaking: true })
    expect(at(6000).interviewerAudible(false)).toBeNull()
  })

  it('forgets a cut-in that no flush followed, so a later flush is not timed from it', () => {
    const { at } = setup()
    at(4000).interviewerAudible(true)
    at(5000).vad({ speaking: true }) // a quick "mm-hmm" the server let pass
    at(6000).interviewerAudible(false)
    at(7000).interviewerAudible(true)
    at(9000).flushed() // the server heard a cut-in this VAD missed
    expect(at(9100).interviewerAudible(false)).toBeNull()
  })

  it('gives nothing for a flush with no cut-in heard', () => {
    const { at } = setup()
    at(4000).interviewerAudible(true)
    at(5100).flushed()
    expect(at(5180).interviewerAudible(false)).toBeNull()
  })

  it('does not count speech while the interviewer is quiet as a cut-in', () => {
    const { at } = setup()
    at(5000).vad({ speaking: true })
    at(6000).interviewerAudible(true)
    at(6100).flushed()
    expect(at(6180).interviewerAudible(false)).toBeNull()
  })

  it('drops a flush that came while the interviewer was quiet once it is heard again', () => {
    const { at } = setup()
    at(5100).flushed()
    at(6000).interviewerAudible(true)
    at(6500).vad({ speaking: true })
    expect(at(8000).interviewerAudible(false)).toBeNull()
  })

  it('is given once per cut-in', () => {
    const { at } = setup()
    at(4000).interviewerAudible(true)
    at(5000).vad({ speaking: true })
    at(5100).flushed()
    expect(at(5180).interviewerAudible(false)).not.toBeNull()
    at(7000).interviewerAudible(true)
    expect(at(8000).interviewerAudible(false)).toBeNull()
  })
})

it('keeps the two numbers apart within one turn', () => {
  const { at } = setup(0)
  at(1000).vad({ speaking: false, quietMs: 300 })
  expect(at(2000).interviewerAudible(true)).toEqual({ stop_to_audible_ms: 1300 })
  at(3000).vad({ speaking: true })
  at(3100).flushed()
  expect(at(3150).interviewerAudible(false)).toEqual({ interrupt_to_silence_ms: 150 })
  at(4000).vad({ speaking: false, quietMs: 300 })
  expect(at(5000).interviewerAudible(true)).toEqual({ stop_to_audible_ms: 1300 })
})
