import { describe, expect, it } from 'vitest'
import { parseRoute, reportHash, screenFor, startHolds } from './route'

const ID = '0b6f2c1e-4a7d-4e7b-9a51-3f2d8c6b1a90'

describe('parseRoute', () => {
  it.each(['', '#', '#/'])('gives the landing page for %j', (hash) => {
    expect(parseRoute(hash)).toEqual({ page: 'landing' })
  })

  it('gives the interview page for #/interview', () => {
    expect(parseRoute('#/interview')).toEqual({ page: 'interview' })
  })

  it('gives the report page and its id for #/report/{id}', () => {
    expect(parseRoute(`#/report/${ID}`)).toEqual({ page: 'report', id: ID })
  })

  it.each([
    ['an unknown page', '#/settings'],
    ['the interview page with more after it', '#/interview/again'],
    ['a report with no id', '#/report'],
    ['a report with an empty id', '#/report/'],
    ['a report id with a slash in it', `#/report/${ID}/extra`],
    ['a report id with characters an id never has', '#/report/<script>'],
    ['a path without the leading slash', '#interview'],
  ])('falls back to the landing page for %s', (_name, hash) => {
    expect(parseRoute(hash)).toEqual({ page: 'landing' })
  })
})

describe('reportHash', () => {
  it('builds the hash that parseRoute reads back', () => {
    expect(reportHash(ID)).toBe(`#/report/${ID}`)
    expect(parseRoute(reportHash(ID))).toEqual({ page: 'report', id: ID })
  })
})

describe('screenFor', () => {
  const interview = { page: 'interview' } as const

  it('shows the interview only after a Start, on a computer', () => {
    expect(screenFor(interview, true, false)).toEqual({ page: 'interview' })
  })

  it.each([
    ['a reload or a typed address, with no Start', false, false],
    ['a phone, even after a Start', true, true],
  ])('sends %s back to the first page', (_case, started, phone) => {
    expect(screenFor(interview, started, phone)).toEqual({ page: 'landing', redirect: true })
  })

  it('shows a report whether or not there was a Start', () => {
    const report = { page: 'report', id: ID } as const
    expect(screenFor(report, false, false)).toEqual(report)
    expect(screenFor(report, true, true)).toEqual(report)
  })

  it('shows the first page where it is, with nothing to put back', () => {
    const shown = screenFor({ page: 'landing' }, false, true)
    expect(shown).toEqual({ page: 'landing', redirect: false })
  })
})

describe('startHolds', () => {
  it('is used up by leaving the interview page', () => {
    expect(startHolds('interview', 'report')).toBe(false)
    expect(startHolds('interview', 'landing')).toBe(false)
  })

  it('holds on the way to the interview page and while on it', () => {
    expect(startHolds('landing', 'interview')).toBe(true)
    expect(startHolds('interview', 'interview')).toBe(true)
    expect(startHolds('report', 'landing')).toBe(true)
  })
})
