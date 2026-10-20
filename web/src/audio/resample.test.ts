import { describe, expect, it } from 'vitest'
import { LinearResampler } from './resample'

// A repeatable stand-in for audio: a small linear congruential generator, so no test is random.
function noise(length: number, seed = 1): Float32Array {
  const out = new Float32Array(length)
  let s = seed
  for (let i = 0; i < length; i++) {
    s = (s * 1664525 + 1013904223) % 4294967296
    out[i] = (s / 4294967296) * 2 - 1
  }
  return out
}

function sine(freq: number, rate: number, seconds: number): Float32Array {
  const out = new Float32Array(Math.round(rate * seconds))
  for (let i = 0; i < out.length; i++) out[i] = Math.sin((2 * Math.PI * freq * i) / rate)
  return out
}

function zeroCrossings(x: Float32Array): number {
  let n = 0
  for (let i = 1; i < x.length; i++) if (x[i - 1] < 0 !== x[i] < 0) n++
  return n
}

function pushInChunks(r: LinearResampler, x: Float32Array, sizes: number[]): Float32Array {
  const parts: Float32Array[] = []
  let at = 0
  for (let k = 0; at < x.length; k++) {
    const size = sizes[k % sizes.length]
    parts.push(r.push(x.subarray(at, at + size)))
    at += size
  }
  const out = new Float32Array(parts.reduce((n, p) => n + p.length, 0))
  let w = 0
  for (const p of parts) {
    out.set(p, w)
    w += p.length
  }
  return out
}

describe('LinearResampler', () => {
  it('turns 4800 samples at 48 kHz into 1600 at 16 kHz, give or take one', () => {
    const out = new LinearResampler(48000, 16000).push(noise(4800))
    expect(Math.abs(out.length - 1600)).toBeLessThanOrEqual(1)
  })

  it('turns 1600 samples at 16 kHz into about 4800 at 48 kHz', () => {
    const out = new LinearResampler(16000, 48000).push(noise(1600))
    expect(Math.abs(out.length - 4800)).toBeLessThanOrEqual(3)
  })

  it('starts on the first input sample and lands on every third one when going 48 kHz to 16 kHz', () => {
    const x = noise(30)
    const out = new LinearResampler(48000, 16000).push(x)
    expect(Array.from(out)).toEqual([x[0], x[3], x[6], x[9], x[12], x[15], x[18], x[21], x[24], x[27]])
  })

  it('interpolates halfway between two samples when going 16 kHz to 32 kHz', () => {
    const out = new LinearResampler(16000, 32000).push(new Float32Array([0, 1, 0, -1]))
    expect(Array.from(out)).toEqual([0, 0.5, 1, 0.5, 0, -0.5])
  })

  it.each([
    ['48000 to 16000', 48000, 16000],
    ['44100 to 16000', 44100, 16000],
    ['16000 to 48000', 16000, 48000],
    ['16000 to 44100', 16000, 44100],
  ])('gives the same samples chunked as in one go (%s)', (_name, inRate, outRate) => {
    const x = noise(9000, 7)
    const whole = new LinearResampler(inRate, outRate).push(x)
    // Odd sizes, including 1, so chunk edges fall on every part of the sample grid.
    const chunked = pushInChunks(new LinearResampler(inRate, outRate), x, [1, 2, 3, 128, 441, 5, 1, 1000, 7])
    expect(chunked.length).toBe(whole.length)
    for (let i = 0; i < whole.length; i++) expect(Math.abs(chunked[i] - whole[i])).toBeLessThan(1e-6)
  })

  it('ignores an empty chunk in the middle of a stream', () => {
    const x = noise(1000, 3)
    const whole = new LinearResampler(44100, 16000).push(x)
    const r = new LinearResampler(44100, 16000)
    const a = r.push(x.subarray(0, 400))
    expect(r.push(new Float32Array(0)).length).toBe(0)
    const b = r.push(x.subarray(400))
    expect([...a, ...b]).toEqual(Array.from(whole))
  })

  it('does not drift over a long stream pushed one sample at a time', () => {
    const x = noise(44100, 11)
    const r = new LinearResampler(44100, 16000)
    let total = 0
    for (let i = 0; i < x.length; i++) total += r.push(x.subarray(i, i + 1)).length
    // One second in is one second out, less the sample held back for interpolation.
    expect(Math.abs(total - 16000)).toBeLessThanOrEqual(1)
  })

  it('keeps the pitch of a 1 kHz sine through 16 kHz to 48 kHz and back', () => {
    const original = sine(1000, 16000, 1)
    const up = new LinearResampler(16000, 48000).push(original)
    const back = new LinearResampler(48000, 16000).push(up)
    const expected = zeroCrossings(original)
    expect(expected).toBeGreaterThan(1900)
    for (const result of [up, back]) {
      const crossings = zeroCrossings(result)
      expect(Math.abs(crossings - expected) / expected).toBeLessThan(0.02)
    }
  })

  it('keeps the pitch of a 1 kHz sine from 44.1 kHz down to 16 kHz', () => {
    const original = sine(1000, 44100, 1)
    const down = new LinearResampler(44100, 16000).push(original)
    expect(Math.abs(zeroCrossings(down) - 2000) / 2000).toBeLessThan(0.02)
  })

  it('rejects a rate that is not a positive whole number', () => {
    for (const bad of [0, -48000, 44100.5, NaN, Infinity]) {
      expect(() => new LinearResampler(bad, 16000)).toThrow(RangeError)
      expect(() => new LinearResampler(16000, bad)).toThrow(RangeError)
    }
  })
})
