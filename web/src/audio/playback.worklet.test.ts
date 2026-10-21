// The playback worklet: it plays pushed 16 kHz frames at the context's rate once 100 ms is
// queued (the jitter buffer), drops everything at once on a flush (barge-in), and says when the
// interviewer starts and stops speaking. These drive its process() as the browser would, at
// 48 kHz, where 0.4 s is 150 render quanta.

import { readFileSync } from 'node:fs'
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { loadWorklet, type FakeProcessor } from './fake-worklet-scope'
import { FRAME_SAMPLES } from './pcm'
import type { PlaybackCommand, PlaybackEvent } from './playback.worklet'

const RATE = 48000
const QUANTUM = 128
const QUIET_QUANTA = (0.4 * RATE) / QUANTUM // 150
// 100 ms at 48 kHz is 4800 samples. Five 20 ms frames come out as 4797 for now (the resampler
// holds the last one back until its neighbour comes), so six is the fewest that start playback.
const PREROLL_SAMPLES = 0.1 * RATE
const PREROLL_FRAMES = 6
const SAMPLES_PER_FRAME = FRAME_SAMPLES * 3 // at 48 kHz

let makePlayback: (rate: number) => FakeProcessor

beforeAll(async () => {
  makePlayback = await loadWorklet('playback', () => import('./playback.worklet'))
})

afterAll(() => {
  vi.unstubAllGlobals()
})

function send(player: FakeProcessor, command: PlaybackCommand): void {
  player.port.deliver(command)
}

/** Push `frames` 20 ms frames, every sample `value` (as int16). */
function push(player: FakeProcessor, frames: number, value: number): void {
  for (let f = 0; f < frames; f++) {
    send(player, { type: 'push', pcm: new Int16Array(FRAME_SAMPLES).fill(value) })
  }
}

/** Run `quanta` render quanta, and give each one's output. */
function render(player: FakeProcessor, quanta: number): Float32Array[] {
  const blocks: Float32Array[] = []
  for (let k = 0; k < quanta; k++) {
    const out = new Float32Array(QUANTUM)
    expect(player.process([], [[out]])).toBe(true)
    blocks.push(out)
  }
  return blocks
}

function joined(blocks: Float32Array[]): number[] {
  return blocks.flatMap((b) => Array.from(b))
}

function speakingEvents(player: FakeProcessor): boolean[] {
  return player.port.posted
    .map((p) => p.message as PlaybackEvent)
    .filter((m) => m.type === 'speaking')
    .map((m) => m.speaking)
}

const LOUD = 8192 // 0.25 of full scale
const QUARTER = LOUD / 32767

/** How many samples of `out` are not zero. */
function sounding(out: number[]): number {
  return out.filter((s) => s !== 0).length
}

describe('the playback worklet', () => {
  it('plays queued audio at three times the rate once 100 ms is queued, then silence', () => {
    const player = makePlayback(RATE)
    push(player, PREROLL_FRAMES, LOUD)
    const out = joined(render(player, 50))
    // 6 x 320 samples at 16 kHz are 5757 at 48 kHz for now: the last one waits for its neighbour.
    const played = PREROLL_FRAMES * SAMPLES_PER_FRAME - 3
    for (const s of out.slice(0, played)) expect(s).toBeCloseTo(QUARTER, 6)
    expect(out.slice(played).every((s) => s === 0)).toBe(true)
  })

  it('plays nothing while less than 100 ms is queued, then all of it from the start', () => {
    const player = makePlayback(RATE)
    push(player, PREROLL_FRAMES - 1, LOUD)
    expect(PREROLL_FRAMES * SAMPLES_PER_FRAME - SAMPLES_PER_FRAME - 3).toBeLessThan(PREROLL_SAMPLES)
    expect(sounding(joined(render(player, 100)))).toBe(0) // waiting is not dropping

    push(player, 1, LOUD)
    expect(sounding(joined(render(player, 100)))).toBe(PREROLL_FRAMES * SAMPLES_PER_FRAME - 3)
  })

  it('waits for 100 ms again after it runs dry', () => {
    // A stall longer than what is queued empties the ring. Playing the next frames as they
    // trickle in would stutter; waiting for a full pre-roll plays them in one piece.
    const player = makePlayback(RATE)
    push(player, PREROLL_FRAMES, LOUD)
    render(player, 50) // all of it played, and then some silence
    push(player, 2, LOUD)
    expect(sounding(joined(render(player, 50)))).toBe(0)
    push(player, PREROLL_FRAMES - 2, LOUD)
    expect(sounding(joined(render(player, 50)))).toBe(PREROLL_FRAMES * SAMPLES_PER_FRAME)
  })

  it("plays Vapi's recorded arrival times without a gap once it starts", () => {
    // fixtures/vapi/ws_binary_frames_first20.json: 0.4 s of the opening line, arriving in bursts
    // of 2 to 4 frames with gaps of up to 0.14 s. Each render quantum runs at its own time and
    // gets every frame that arrived by then. Without a pre-roll the ring runs dry for about
    // 43 ms at t = 0.30 s, which plays as a hole in the middle of the opening line.
    const fixture = new URL(
      '../../../server/tests/fixtures/vapi/ws_binary_frames_first20.json',
      import.meta.url,
    )
    const arrivals = (JSON.parse(readFileSync(fixture, 'utf8')) as { t: number }[]).map((f) => f.t)
    const player = makePlayback(RATE)
    const out: number[] = []
    let next = 0
    for (let k = 0; k < 0.8 * (RATE / QUANTUM); k++) {
      const now = 0.15 + (k * QUANTUM) / RATE
      for (; next < arrivals.length && arrivals[next] <= now; next++) push(player, 1, LOUD)
      out.push(...joined(render(player, 1)))
    }
    expect(next).toBe(arrivals.length)

    const first = out.findIndex((s) => s !== 0)
    const last = out.findLastIndex((s) => s !== 0)
    // Every sample that arrived, in one run.
    expect(last - first + 1).toBe(arrivals.length * SAMPLES_PER_FRAME - 3)
    expect(sounding(out.slice(first, last + 1))).toBe(last - first + 1)
  })

  it('plays the same audio on every output channel', () => {
    const player = makePlayback(RATE)
    push(player, PREROLL_FRAMES, LOUD)
    const left = new Float32Array(QUANTUM)
    const right = new Float32Array(QUANTUM)
    player.process([], [[left, right]])
    expect(right).toEqual(left)
    expect(left[0]).toBeCloseTo(QUARTER, 6)
  })

  it('plays silence when nothing is queued', () => {
    const player = makePlayback(RATE)
    expect(joined(render(player, 5)).every((s) => s === 0)).toBe(true)
  })

  it('holds 2 s at most and drops what comes past that', () => {
    const player = makePlayback(RATE)
    push(player, 150, LOUD) // 3 s
    const out = joined(render(player, (3 * RATE) / QUANTUM))
    expect(out.filter((s) => s !== 0)).toHaveLength(2 * RATE)
  })

  describe('flush', () => {
    it('drops everything queued at once', () => {
      const player = makePlayback(RATE)
      push(player, 50, LOUD) // 1 s
      render(player, 1)
      send(player, { type: 'flush' })
      expect(joined(render(player, 400)).every((s) => s === 0)).toBe(true)
    })

    it('starts the next audio afresh, without the sample held back from the dropped audio', () => {
      const player = makePlayback(RATE)
      push(player, 1, LOUD)
      send(player, { type: 'flush' })
      // Kept, the old resampler would start this silence from the dropped 0.25. Enough of it is
      // pushed to start playback, so the check is on audio that really plays.
      push(player, PREROLL_FRAMES, 0)
      expect(joined(render(player, 50)).every((s) => s === 0)).toBe(true)
    })

    it('waits for 100 ms again before the next reply plays', () => {
      const player = makePlayback(RATE)
      push(player, 50, LOUD)
      render(player, 1) // playing
      send(player, { type: 'flush' })
      push(player, 2, LOUD)
      expect(sounding(joined(render(player, 50)))).toBe(0)
      push(player, PREROLL_FRAMES - 2, LOUD)
      expect(sounding(joined(render(player, 50)))).toBe(PREROLL_FRAMES * SAMPLES_PER_FRAME - 3)
    })

    it('says the interviewer stopped speaking at once', () => {
      const player = makePlayback(RATE)
      push(player, 50, LOUD)
      render(player, 1)
      expect(speakingEvents(player)).toEqual([true])
      send(player, { type: 'flush' })
      expect(speakingEvents(player)).toEqual([true, false])
    })

    it('says nothing when the interviewer was not speaking', () => {
      const player = makePlayback(RATE)
      send(player, { type: 'flush' })
      render(player, 10)
      expect(speakingEvents(player)).toEqual([])
    })
  })

  describe('speaking', () => {
    it('turns on with the first loud quantum and off after 0.4 s of quiet', () => {
      const player = makePlayback(RATE)
      push(player, 10, LOUD) // 200 ms
      let lastLoud = -1
      const eventsAfter: boolean[][] = []
      for (let k = 0; k < 400; k++) {
        const [block] = render(player, 1)
        if (block.some((s) => s !== 0)) lastLoud = k
        eventsAfter.push(speakingEvents(player))
      }
      expect(eventsAfter[0]).toEqual([true])
      // 200 ms comes out as 9597 samples for now (the last one waits for its neighbour), which
      // ends in quantum 74, the 75th.
      expect(lastLoud).toBe(74)
      expect(eventsAfter[lastLoud + QUIET_QUANTA - 1]).toEqual([true])
      expect(eventsAfter[lastLoud + QUIET_QUANTA]).toEqual([true, false])
      expect(eventsAfter[399]).toEqual([true, false])
    })

    it('stays on through a pause shorter than 0.4 s', () => {
      const player = makePlayback(RATE)
      push(player, 10, LOUD)
      render(player, 75 + QUIET_QUANTA - 1)
      push(player, 10, LOUD)
      render(player, 20)
      expect(speakingEvents(player)).toEqual([true])
    })

    it('counts as speech only at an RMS of 0.01 or more', () => {
      const quiet = makePlayback(RATE)
      push(quiet, 10, 295) // 0.009
      render(quiet, 100)
      expect(speakingEvents(quiet)).toEqual([])

      const audible = makePlayback(RATE)
      push(audible, 10, 360) // 0.011
      render(audible, 1)
      expect(speakingEvents(audible)).toEqual([true])
    })
  })
})
