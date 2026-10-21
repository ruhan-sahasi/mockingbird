import { describe, expect, it } from 'vitest'
import { isPhone } from './device'

const IPHONE =
  'Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1'
const ANDROID_PHONE =
  'Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Mobile Safari/537.36'
const ANDROID_TABLET =
  'Mozilla/5.0 (Linux; Android 14; SM-X710) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
const MAC_CHROME =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
const WINDOWS_CHROME =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36'
// Since iPadOS 13, Safari on an iPad sends a Mac user agent. Only its touch points give it away.
const IPAD_SAFARI =
  'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15'

describe('isPhone', () => {
  it('is true for an iPhone', () => {
    expect(isPhone(IPHONE, 5, 390)).toBe(true)
  })

  it('is true for an Android phone', () => {
    expect(isPhone(ANDROID_PHONE, 5, 412)).toBe(true)
  })

  it('is false for desktop Chrome', () => {
    expect(isPhone(MAC_CHROME, 0, 1440)).toBe(false)
    expect(isPhone(WINDOWS_CHROME, 0, 1920)).toBe(false)
  })

  it('is false for tablets, which have room for the interview', () => {
    expect(isPhone(IPAD_SAFARI, 5, 820)).toBe(false)
    expect(isPhone(ANDROID_TABLET, 5, 800)).toBe(false)
  })

  it('is true for a narrow touch screen with a user agent it does not know', () => {
    expect(isPhone('SomeNewBrowser/1.0', 5, 390)).toBe(true)
  })

  it('is false for a narrow desktop window with no touch', () => {
    expect(isPhone(MAC_CHROME, 0, 400)).toBe(false)
  })

  it('is false for a wide touch laptop', () => {
    expect(isPhone(WINDOWS_CHROME, 10, 1366)).toBe(false)
  })
})
