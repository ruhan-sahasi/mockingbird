import { describe, expect, it } from 'vitest'
import { formatCountdown, secondsLeft } from './countdown'

describe('formatCountdown', () => {
  it.each([
    [300, '5:00'],
    [1200, '20:00'],
    [61, '1:01'],
    [60, '1:00'],
    [59, '0:59'],
    [9, '0:09'],
    [0, '0:00'],
  ])('shows %i seconds as %s', (seconds, shown) => {
    expect(formatCountdown(seconds)).toBe(shown)
  })

  it('never shows a negative time', () => {
    expect(formatCountdown(-5)).toBe('0:00')
    expect(formatCountdown(NaN)).toBe('0:00')
  })
})

describe('secondsLeft', () => {
  it('is the whole cap at the start', () => {
    expect(secondsLeft(300, 1000, 1000)).toBe(300)
  })

  it('counts a second as gone only once all of it has passed', () => {
    expect(secondsLeft(300, 1000, 1001)).toBe(300)
    expect(secondsLeft(300, 1000, 1999)).toBe(300)
    expect(secondsLeft(300, 1000, 2000)).toBe(299)
    expect(secondsLeft(300, 1000, 2001)).toBe(299)
  })

  it('never shows more than the cap, even read before the start', () => {
    expect(secondsLeft(300, 5000, 1000)).toBe(300)
  })

  it('stops at zero', () => {
    expect(secondsLeft(300, 0, 300_000)).toBe(0)
    expect(secondsLeft(300, 0, 400_000)).toBe(0)
  })
})
