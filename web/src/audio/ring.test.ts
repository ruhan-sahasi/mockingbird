import { describe, expect, it } from 'vitest'
import { FloatRing } from './ring'

const f = (...v: number[]) => new Float32Array(v)

describe('FloatRing', () => {
  it('reads back what was written, in order', () => {
    const ring = new FloatRing(8)
    expect(ring.write(f(1, 2, 3))).toBe(3)
    expect(ring.available).toBe(3)
    const out = new Float32Array(3)
    expect(ring.read(out)).toBe(3)
    expect(Array.from(out)).toEqual([1, 2, 3])
    expect(ring.available).toBe(0)
  })

  it('wraps around the end of its storage on both write and read', () => {
    const ring = new FloatRing(5)
    ring.write(f(1, 2, 3, 4))
    const first = new Float32Array(3)
    ring.read(first)
    expect(Array.from(first)).toEqual([1, 2, 3])

    // Three more samples run past the end of the storage and wrap to the start.
    expect(ring.write(f(5, 6, 7))).toBe(3)
    expect(ring.available).toBe(4)
    const rest = new Float32Array(4)
    expect(ring.read(rest)).toBe(4)
    expect(Array.from(rest)).toEqual([4, 5, 6, 7])
  })

  it('fills the shortfall with zeros and reports the real sample count', () => {
    const ring = new FloatRing(8)
    ring.write(f(1, 2))
    const out = new Float32Array([9, 9, 9, 9, 9])
    expect(ring.read(out)).toBe(2)
    expect(Array.from(out)).toEqual([1, 2, 0, 0, 0])
  })

  it('gives all zeros and a count of 0 when it is empty', () => {
    const ring = new FloatRing(4)
    const out = new Float32Array([9, 9, 9])
    expect(ring.read(out)).toBe(0)
    expect(Array.from(out)).toEqual([0, 0, 0])
  })

  it('empties on clear and works normally afterwards', () => {
    const ring = new FloatRing(4)
    ring.write(f(1, 2, 3))
    ring.read(new Float32Array(2))
    ring.write(f(4, 5)) // the contents now wrap
    ring.clear()
    expect(ring.available).toBe(0)

    const out = new Float32Array([9, 9])
    expect(ring.read(out)).toBe(0)
    expect(Array.from(out)).toEqual([0, 0])

    expect(ring.write(f(7, 8, 9, 10))).toBe(4)
    const again = new Float32Array(4)
    expect(ring.read(again)).toBe(4)
    expect(Array.from(again)).toEqual([7, 8, 9, 10])
  })

  it('keeps what it holds and drops the samples that do not fit', () => {
    const ring = new FloatRing(4)
    expect(ring.write(f(1, 2, 3, 4, 5, 6))).toBe(4)
    expect(ring.write(f(7))).toBe(0)
    expect(ring.available).toBe(4)
    const out = new Float32Array(4)
    ring.read(out)
    expect(Array.from(out)).toEqual([1, 2, 3, 4])
  })

  it('accepts a write again once a read has made room', () => {
    const ring = new FloatRing(4)
    ring.write(f(1, 2, 3, 4))
    ring.read(new Float32Array(1))
    expect(ring.write(f(5, 6))).toBe(1)
    const out = new Float32Array(4)
    ring.read(out)
    expect(Array.from(out)).toEqual([2, 3, 4, 5])
  })

  it('matches a plain queue through many mixed writes and reads', () => {
    const ring = new FloatRing(37)
    const model: number[] = []
    let s = 12345
    const next = (max: number) => {
      s = (s * 1664525 + 1013904223) % 4294967296
      return s % (max + 1)
    }
    let counter = 1
    for (let step = 0; step < 2000; step++) {
      if (next(1) === 0) {
        const chunk = new Float32Array(next(20))
        for (let i = 0; i < chunk.length; i++) chunk[i] = counter++
        const accepted = ring.write(chunk)
        expect(accepted).toBe(Math.min(chunk.length, 37 - model.length))
        model.push(...chunk.subarray(0, accepted))
        // Samples that were refused are gone for good, so the next ones continue the count.
      } else {
        const out = new Float32Array(next(20))
        const got = ring.read(out)
        const expected = model.splice(0, out.length)
        expect(got).toBe(expected.length)
        expect(Array.from(out.subarray(0, got))).toEqual(expected)
        expect(Array.from(out.subarray(got))).toEqual(new Array(out.length - got).fill(0))
      }
      expect(ring.available).toBe(model.length)
    }
  })

  it('rejects a capacity that is not a positive whole number', () => {
    for (const bad of [0, -1, 2.5, NaN]) expect(() => new FloatRing(bad)).toThrow(RangeError)
  })
})
