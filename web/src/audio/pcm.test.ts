import { describe, expect, it } from 'vitest'
import { FRAME_SAMPLES, floatToInt16, int16ToFloat } from './pcm'

describe('floatToInt16', () => {
  it('clamps to the int16 range', () => {
    const out = floatToInt16(new Float32Array([1.5, -2]))
    expect(Array.from(out)).toEqual([32767, -32768])
  })

  it('maps the ends and the middle of the range', () => {
    const out = floatToInt16(new Float32Array([0, 1, -1, 0.5, -0.5]))
    expect(Array.from(out)).toEqual([0, 32767, -32768, 16384, -16384])
  })

  it('turns NaN into silence', () => {
    expect(Array.from(floatToInt16(new Float32Array([NaN])))).toEqual([0])
  })

  it('returns one sample per input sample', () => {
    expect(floatToInt16(new Float32Array(FRAME_SAMPLES)).length).toBe(FRAME_SAMPLES)
    expect(floatToInt16(new Float32Array(0)).length).toBe(0)
  })
})

describe('int16ToFloat', () => {
  it('maps the ends of the range to -1 and 1', () => {
    const out = int16ToFloat(new Int16Array([0, 32767, -32768]))
    expect(Array.from(out)).toEqual([0, 1, -1])
  })

  it('survives a round trip for every int16 value', () => {
    const all = new Int16Array(65536)
    for (let i = 0; i < all.length; i++) all[i] = i - 32768
    expect(Array.from(floatToInt16(int16ToFloat(all)))).toEqual(Array.from(all))
  })
})

describe('FRAME_SAMPLES', () => {
  it('is 20 ms at 16 kHz, which is 640 bytes of s16le', () => {
    expect(FRAME_SAMPLES).toBe(320)
    expect(FRAME_SAMPLES * 2).toBe(640)
    expect(FRAME_SAMPLES / 16000).toBeCloseTo(0.02, 10)
  })
})
