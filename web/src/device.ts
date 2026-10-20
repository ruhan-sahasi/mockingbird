// Whether this looks like a phone. Phones are turned away: the interview needs a mic that is not
// fighting its own speaker, so it wants a laptop or desktop with headphones.

// A phone in portrait is at most about 440 CSS px wide. Tablets and laptops are wider.
const PHONE_MAX_WIDTH = 500

/**
 * `ua` is navigator.userAgent, `maxTouchPoints` is navigator.maxTouchPoints and `width` is the
 * viewport width in CSS pixels. Tablets are not phones, and neither is a desktop window dragged
 * narrow.
 */
export function isPhone(ua: string, maxTouchPoints: number, width: number): boolean {
  if (/iPhone|iPod/.test(ua)) return true
  // Android phones say "Mobile"; Android tablets leave it out.
  if (/Android/.test(ua) && /Mobile/.test(ua)) return true
  // A narrow touch screen is a phone even when its user agent is one we do not know. Touch is
  // required so a desktop browser window made narrow is not mistaken for one.
  return maxTouchPoints > 0 && width < PHONE_MAX_WIDTH
}
