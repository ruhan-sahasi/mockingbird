// The playback worklet: it plays pushed 16 kHz frames at the context's rate, drops everything at
// once on a flush (barge-in), and says when the interviewer starts and stops speaking. These
// drive its process() as the browser would, at 48 kHz, where 0.4 s is 150 render quanta.

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { loadWorklet, type FakeProcessor } from './fake-worklet-scope'
import { FRAME_SAMPLES } from './pcm'
import type { PlaybackCommand, PlaybackEvent } from './playback.worklet'

const RATE = 48000
const QUANTUM = 128
const QUIET_QUANTA = (0.4 * RATE) / QUANTUM // 150

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

describe('the playback worklet', () => {
  it('plays a pushed frame at three times the rate, then silence', () => {
    const player = makePlayback(RATE)
    push(player, 1, LOUD)
    const out = joined(render(player, 10))
    // 320 samples at 16 kHz are 957 at 48 kHz for now: the last one waits for its neighbour.
    for (const s of out.slice(0, 957)) expect(s).toBeCloseTo(QUARTER, 6)
    expect(out.slice(957).every((s) => s === 0)).toBe(true)
  })

  it('plays the same audio on every output channel', () => {
    const player = makePlayback(RATE)
    push(player, 1, LOUD)
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
      // Kept, the old resampler would start this silence from the dropped 0.25.
      push(player, 1, 0)
      expect(joined(render(player, 10)).every((s) => s === 0)).toBe(true)
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
      push(player, 5, LOUD) // 100 ms
      let lastLoud = -1
      const eventsAfter: boolean[][] = []
      for (let k = 0; k < 400; k++) {
        const [block] = render(player, 1)
        if (block.some((s) => s !== 0)) lastLoud = k
        eventsAfter.push(speakingEvents(player))
      }
      expect(eventsAfter[0]).toEqual([true])
      // 100 ms comes out as 4797 samples for now (the last one waits for its neighbour), which
      // ends in quantum 37, the 38th.
      expect(lastLoud).toBe(37)
      expect(eventsAfter[lastLoud + QUIET_QUANTA - 1]).toEqual([true])
      expect(eventsAfter[lastLoud + QUIET_QUANTA]).toEqual([true, false])
      expect(eventsAfter[399]).toEqual([true, false])
    })

    it('stays on through a pause shorter than 0.4 s', () => {
      const player = makePlayback(RATE)
      push(player, 5, LOUD)
      render(player, 38 + QUIET_QUANTA - 1)
      push(player, 5, LOUD)
      render(player, 20)
      expect(speakingEvents(player)).toEqual([true])
    })

    it('counts as speech only at an RMS of 0.01 or more', () => {
      const quiet = makePlayback(RATE)
      push(quiet, 5, 295) // 0.009
      render(quiet, 60)
      expect(speakingEvents(quiet)).toEqual([])

      const audible = makePlayback(RATE)
      push(audible, 5, 360) // 0.011
      render(audible, 1)
      expect(speakingEvents(audible)).toEqual([true])
    })
  })
})
