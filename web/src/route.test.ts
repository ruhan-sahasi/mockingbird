import { describe, expect, it } from 'vitest'
import { parseRoute, reportHash } from './route'

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
