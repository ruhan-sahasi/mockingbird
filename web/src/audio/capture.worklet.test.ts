// The capture worklet must post a frame every 20 ms whatever the mic gives it, silence and no
// input at all included, because the server ends the call after 2 s without a frame (relay.py's
// browser_timeout_s, the Task 9 handoff). Its VAD adds a message when the candidate starts or
// stops speaking, and never holds a frame back. These drive its process() as the browser would.

import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest'
import type { CaptureMessage } from './capture.worklet'
import { loadWorklet, type FakeProcessor, type Posted } from './fake-worklet-scope'
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
function run(rate: number, quanta: number, input: Input): Posted[] {
  const capture = makeCapture(rate)
  for (let k = 0; k < quanta; k++) {
    // Returning false would let the browser stop running the node, and the frames would stop.
    expect(capture.process(input(k, rate), [[new Float32Array(QUANTUM)]])).toBe(true)
  }
  return capture.port.posted
}

const messageOf = (p: Posted) => p.message as CaptureMessage

/** The frames among what was posted, as each was posted. */
function frames(posted: Posted[]): { pcm: Int16Array; transfer: Posted['transfer'] }[] {
  return posted.flatMap((p) => {
    const message = messageOf(p)
    return message.type === 'frame' ? [{ pcm: message.pcm, transfer: p.transfer }] : []
  })
}

describe('the capture worklet', () => {
  describe.each(INPUTS)('given %s', (_name, input) => {
    it.each(RUNS)('at %i Hz, %i quanta post %i frames of 320 samples', (rate, quanta, n) => {
      const posted = run(rate, quanta, input)
      const sent = frames(posted)
      expect(sent).toHaveLength(n)
      for (const { pcm, transfer } of sent) {
        expect(pcm).toBeInstanceOf(Int16Array)
        expect(pcm.length).toBe(FRAME_SAMPLES)
        expect(pcm.byteLength).toBe(640)
        // Its buffer is handed over, not copied, and is the frame's alone.
        expect(transfer).toEqual([pcm.buffer])
        expect(pcm.byteOffset).toBe(0)
        expect(pcm.buffer.byteLength).toBe(640)
      }
      // A transferred buffer can not be sent again, so every frame needs its own.
      expect(new Set(sent.map((f) => f.pcm.buffer)).size).toBe(n)
      // Everything else it posted is a VAD change, which hands nothing over.
      for (const p of posted.filter((p) => messageOf(p).type !== 'frame')) {
        expect(messageOf(p).type).toBe('vad')
        expect(p.transfer).toBeUndefined()
      }
    })
  })

  it('sends silence as zeros, and no VAD change', () => {
    for (const [, input] of INPUTS.slice(0, 3)) {
      const posted = run(48000, 375, input)
      expect(posted).toHaveLength(50)
      for (const { pcm } of frames(posted)) {
        expect(pcm.every((s) => s === 0)).toBe(true)
      }
    }
  })

  it('sends what the mic hears', () => {
    const posted = run(16000, 5, () => [[new Float32Array(QUANTUM).fill(0.5)]])
    expect(messageOf(posted[0])).toEqual({ type: 'frame', pcm: expect.any(Int16Array) })
    expect(Array.from(frames(posted)[0].pcm)).toEqual(Array(FRAME_SAMPLES).fill(16384))
  })

  it('keeps the stream even when the input comes and goes', () => {
    // A mic that drops out every other quantum still gives a frame every 20 ms.
    const posted = run(48000, 375, (k, rate) => (k % 2 === 0 ? [[tone(k, rate)]] : []))
    expect(frames(posted)).toHaveLength(50)
  })

  it('posts a VAD change right after the frame that made it, and keeps posting frames', () => {
    // At 16 kHz a frame is input samples 320f to 320f + 319. Quanta 50 to 74 are loud, which is
    // frames 20 to 29: speaking after the third of them (frame 22), and stopped after 300 ms
    // (15 frames) of quiet, at frame 44.
    const loud = (k: number) => k >= 50 && k < 75
    const posted = run(16000, 150, (k) => [[new Float32Array(QUANTUM).fill(loud(k) ? 0.5 : 0)]])

    expect(frames(posted)).toHaveLength(59)
    const vad = posted.flatMap((p, i) => (messageOf(p).type === 'vad' ? [{ i, ...p }] : []))
    expect(vad.map((p) => p.message)).toEqual([
      { type: 'vad', speaking: true, loudMs: 60 },
      { type: 'vad', speaking: false, quietMs: 300 },
    ])
    // Frames 0 to 22 come before the first, and frames 23 to 44 between the two.
    expect(vad.map((p) => p.i)).toEqual([23, 46])
    expect(vad.map((p) => p.transfer)).toEqual([undefined, undefined])
  })
})
