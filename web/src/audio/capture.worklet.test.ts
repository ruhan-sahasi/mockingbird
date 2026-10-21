// The capture worklet must post a frame every 20 ms whatever the mic gives it, silence and no
// input at all included, because the server ends the call after 2 s without a frame (relay.py's
// browser_timeout_s, the Task 9 handoff). These drive its process() as the browser would.

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import { loadWorklet, type FakeProcessor } from './fake-worklet-scope'
import { FRAME_SAMPLES } from './pcm'

const QUANTUM = 128

let makeCapture: (rate: number) => FakeProcessor

beforeAll(async () => {
  makeCapture = await loadWorklet('capture', () => import('./capture.worklet'))
})

afterAll(() => {
  vi.unstubAllGlobals()
})

// What the browser can hand process() for one render quantum: block k of a stream at a rate.
type Input = (k: number, rate: number) => Float32Array[][]
const INPUTS: [string, Input][] = [
  ['no input at all', () => []],
  ['an input with no channels', () => [[]]],
  ['a block of zeros, as from a muted mic', () => [[new Float32Array(QUANTUM)]]],
  ['a 440 Hz tone', (k, rate) => [[tone(k, rate)]]],
]

// [device rate, render quanta fed, frames expected]. The resampler holds back its latest input
// sample, so 16 kHz needs one quantum past a whole second for the 50th frame.
const RUNS: [number, number, number][] = [
  [16000, 5, 1], // 639 samples out: one frame, and most of the next
  [16000, 126, 50],
  [48000, 375, 50], // 1 s
  [44100, 345, 50],
  [48000, 3750, 500], // 10 s: still one frame per 20 ms, with no drift
]

// Block k of a 440 Hz tone at half scale.
function tone(k: number, rate: number): Float32Array {
  const block = new Float32Array(QUANTUM)
  for (let i = 0; i < QUANTUM; i++) {
    block[i] = 0.5 * Math.sin((2 * Math.PI * 440 * (k * QUANTUM + i)) / rate)
  }
  return block
}

/** A capture processor at `rate`, fed `quanta` render quanta. Gives what it posted. */
function run(rate: number, quanta: number, input: Input) {
  const capture = makeCapture(rate)
  for (let k = 0; k < quanta; k++) {
    // Returning false would let the browser stop running the node, and the frames would stop.
    expect(capture.process(input(k, rate), [[new Float32Array(QUANTUM)]])).toBe(true)
  }
  return capture.port.posted
}

describe('the capture worklet', () => {
  describe.each(INPUTS)('given %s', (_name, input) => {
    it.each(RUNS)('at %i Hz, %i quanta post %i frames of 320 samples', (rate, quanta, frames) => {
      const posted = run(rate, quanta, input)
      expect(posted).toHaveLength(frames)
      for (const { message, transfer } of posted) {
        expect(message).toBeInstanceOf(Int16Array)
        const pcm = message as Int16Array
        expect(pcm.length).toBe(FRAME_SAMPLES)
        expect(pcm.byteLength).toBe(640)
        // Its buffer is handed over, not copied, and is the frame's alone.
        expect(transfer).toEqual([pcm.buffer])
        expect(pcm.byteOffset).toBe(0)
        expect(pcm.buffer.byteLength).toBe(640)
      }
      // A transferred buffer can not be sent again, so every frame needs its own.
      expect(new Set(posted.map((p) => (p.message as Int16Array).buffer)).size).toBe(frames)
    })
  })

  it('sends silence as zeros', () => {
    for (const [, input] of INPUTS.slice(0, 3)) {
      const posted = run(48000, 375, input)
      expect(posted).toHaveLength(50)
      for (const { message } of posted) {
        expect((message as Int16Array).every((s) => s === 0)).toBe(true)
      }
    }
  })

  it('sends what the mic hears', () => {
    const posted = run(16000, 5, () => [[new Float32Array(QUANTUM).fill(0.5)]])
    expect(Array.from(posted[0].message as Int16Array)).toEqual(Array(FRAME_SAMPLES).fill(16384))
  })

  it('keeps the stream even when the input comes and goes', () => {
    // A mic that drops out every other quantum still gives a frame every 20 ms.
    const posted = run(48000, 375, (k, rate) => (k % 2 === 0 ? [[tone(k, rate)]] : []))
    expect(posted).toHaveLength(50)
  })
})
