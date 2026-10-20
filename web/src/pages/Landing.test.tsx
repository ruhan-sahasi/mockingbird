import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import Landing from './Landing'

const CONSENT =
  'Your voice is sent to Vapi and Claude to run and grade this interview. Nothing is shared publicly.'
const PHONE = 'Mockingbird needs a laptop or desktop browser with a mic. Open this link there.'

function render(props: Partial<Parameters<typeof Landing>[0]> = {}): string {
  return renderToStaticMarkup(<Landing phone={false} limit={null} onStart={() => {}} {...props} />)
    .replace(/&#x27;/g, "'")
}

describe('Landing', () => {
  it('asks for consent in the exact words', () => {
    expect(render()).toContain(CONSENT)
  })

  it('tells the visitor to use headphones', () => {
    expect(render()).toMatch(/headphones/i)
  })

  it('starts with the Start button disabled, since consent is not given yet', () => {
    const html = render()
    expect(html).toMatch(/<input[^>]*type="checkbox"(?![^>]*checked)[^>]*>/)
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Start the interview<\/button>/)
  })

  it('shows a phone the laptop message in place of Start', () => {
    const html = render({ phone: true })
    expect(html).toContain(PHONE)
    expect(html).not.toContain('<button')
  })

  it.each([
    ['visitor_limit', "You've used today's demo. Come back tomorrow."],
    ['daily_budget', 'The demo is resting for today. Come back tomorrow.'],
  ] as const)('shows the %s message', (limit, message) => {
    expect(render({ limit })).toContain(message)
  })

  it('shows no limit message when there is no limit', () => {
    const html = render()
    expect(html).not.toContain('Come back tomorrow.')
  })
})
