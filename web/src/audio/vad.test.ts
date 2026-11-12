// The candidate's speech, found by loudness alone, on synthetic 20 ms frames at 16 kHz.

import { describe, expect, it } from 'vitest'
import { FRAME_SAMPLES } from './pcm'
import { EnergyVad } from './vad'

/** One 20 ms frame whose every sample is `level`, so its RMS is |level|. */
function frame(level: number): Float32Array {
  return new Float32Array(FRAME_SAMPLES).fill(level)
}

const LOUD = frame(0.1)
const QUIET = frame(0.001)

/** Every event the VAD gives for `frames`, in order, with the index of the frame that gave it. */
function feed(vad: EnergyVad, frames: Float32Array[]) {
  const events = []
  for (const [i, f] of frames.entries()) {
    const event = vad.push(f)
    if (event !== null) events.push({ at: i, ...event })
  }
  return events
}

const times = (n: number, f: Float32Array) => Array<Float32Array>(n).fill(f)

describe('EnergyVad', () => {
  it('says speaking after silence and 3 loud frames, with how long they were loud, once', () => {
    const vad = new EnergyVad()
    expect(feed(vad, [...times(10, QUIET), ...times(10, LOUD)])).toEqual([
      { at: 12, speaking: true, loudMs: 60 },
    ])
  })

  it('says stopped after 300 ms of quiet, with how long it was quiet', () => {
    const vad = new EnergyVad()
    feed(vad, times(5, LOUD))
    expect(feed(vad, times(20, QUIET))).toEqual([{ at: 14, speaking: false, quietMs: 300 }])
  })

  it('gives no event for 2 loud frames and then quiet', () => {
    const vad = new EnergyVad()
    expect(feed(vad, [...times(2, LOUD), ...times(30, QUIET), ...times(2, LOUD)])).toEqual([])
  })

  it('rides out a gap between words shorter than the quiet time', () => {
    const vad = new EnergyVad()
    const words = [...times(3, LOUD), ...times(14, QUIET), ...times(3, LOUD), ...times(15, QUIET)]
    expect(feed(vad, words)).toEqual([
      { at: 2, speaking: true, loudMs: 60 },
      { at: 34, speaking: false, quietMs: 300 },
    ])
  })

  it('starts at an RMS of 0.02 by default', () => {
    expect(feed(new EnergyVad(), times(10, frame(0.019)))).toEqual([])
    expect(feed(new EnergyVad(), times(10, frame(-0.021)))).toEqual([
      { at: 2, speaking: true, loudMs: 60 },
    ])
  })

  it('counts a frame exactly at the threshold as loud', () => {
    // 0.125 is exact in float32, so the RMS lands on the threshold and not a hair below it.
    const vad = new EnergyVad({ speechRms: 0.125 })
    expect(feed(vad, times(3, frame(-0.125)))).toEqual([{ at: 2, speaking: true, loudMs: 60 }])
  })

  it('takes its thresholds and frame length from the options', () => {
    const vad = new EnergyVad({ speechRms: 0.5, startFrames: 1, quietMs: 100, frameMs: 50 })
    expect(feed(vad, [LOUD, frame(0.5), QUIET, QUIET])).toEqual([
      { at: 1, speaking: true, loudMs: 50 },
      { at: 3, speaking: false, quietMs: 100 },
    ])
  })

  it('treats an empty frame as quiet', () => {
    const vad = new EnergyVad({ startFrames: 1, quietMs: 20 })
    expect(feed(vad, [LOUD, new Float32Array(0)])).toEqual([
      { at: 0, speaking: true, loudMs: 20 },
      { at: 1, speaking: false, quietMs: 20 },
    ])
  })
})
