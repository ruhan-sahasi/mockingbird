import { renderToStaticMarkup } from 'react-dom/server'
import { describe, expect, it } from 'vitest'
import type { ItemReport, Report, ReportResult } from '../api'
import { ReportView } from './Report'

const QUESTION = 'Tell me about a time you got feedback you did not agree with.'

// The shapes report.py's verify_quotes and build_report produce.
const SCORED: ItemReport = {
  item: 'action',
  score: 3,
  rationale: 'Several concrete steps, and it is clear which were the candidate’s.',
  quotes: ['I wrote the cutover plan myself', 'I paired with the on-call engineer'],
  dropped_quotes: ['I fixed the whole company'],
  status: 'scored',
}

const NO_EVIDENCE: ItemReport = {
  item: 'task',
  score: null,
  rationale: 'The goal is implied but the candidate’s part is unclear.',
  quotes: [],
  dropped_quotes: ['What was your part in the cutover'],
  status: 'not_enough_evidence',
}

function graded(...items: ItemReport[]): Report {
  return { status: 'graded', questions: [{ question_idx: 0, question_text: QUESTION, items }] }
}

// The text a reader sees: the markup with its tags taken out and its spacing collapsed.
function render(result: ReportResult | null): { html: string; text: string } {
  const html = renderToStaticMarkup(<ReportView result={result} />)
  const text = html
    .replace(/<[^>]+>/g, ' ')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .replace(/\s+/g, ' ')
    .trim()
  return { html, text }
}

describe('ReportView', () => {
  it('shows the question with its number and text', () => {
    const { text } = render(graded(SCORED))
    expect(text).toContain('Question 1')
    expect(text).toContain(QUESTION)
  })

  it('shows a scored item with its name, score out of 4, rationale and every kept quote', () => {
    const { text } = render(graded(SCORED))
    expect(text).toContain('Action')
    expect(text).toContain('3 of 4')
    expect(text).toContain(SCORED.rationale)
    for (const quote of SCORED.quotes) expect(text).toContain(quote)
    expect(text).not.toContain('Not enough evidence')
  })

  it('never shows a quote that failed the check against the transcript', () => {
    const { text } = render(graded(SCORED, NO_EVIDENCE))
    expect(text).not.toContain('I fixed the whole company')
    expect(text).not.toContain('What was your part in the cutover')
  })

  it('shows "Not enough evidence" and no score for an item with no kept quote', () => {
    const { text } = render(graded(NO_EVIDENCE))
    expect(text).toContain('Task')
    expect(text).toContain('Not enough evidence')
    expect(text).not.toMatch(/\d of 4/)
    expect(text).toContain(NO_EVIDENCE.rationale)
  })

  it('names all six rubric items in plain words', () => {
    const items = ['situation', 'task', 'action', 'result', 'specificity', 'ownership'].map(
      (item): ItemReport => ({ ...SCORED, item }),
    )
    const { text } = render(graded(...items))
    for (const name of ['Situation', 'Task', 'Action', 'Result', 'Specificity', 'Ownership']) {
      expect(text).toContain(name)
    }
  })

  it('numbers questions from their index, counting from 1', () => {
    const report: Report = {
      status: 'graded',
      questions: [
        { question_idx: 0, question_text: 'First question?', items: [SCORED] },
        { question_idx: 1, question_text: 'Second question?', items: [SCORED] },
      ],
    }
    const { text } = render(report)
    expect(text.indexOf('Question 1')).toBeLessThan(text.indexOf('First question?'))
    expect(text.indexOf('Question 2')).toBeLessThan(text.indexOf('Second question?'))
  })

  it('says in one sentence that no answer was recorded', () => {
    const { text } = render({ status: 'no_answers', questions: [] })
    expect(text).toContain('No answer was recorded, so there was nothing to grade.')
    expect(text).not.toContain('Question 1')
  })

  it('says in one sentence that grading failed', () => {
    const { text } = render({ status: 'grading_failed', questions: [] })
    expect(text).toContain('The grader could not score this interview.')
    expect(text).not.toContain('Question 1')
  })

  it('says nothing but the title before the server has answered', () => {
    const { text } = render(null)
    expect(text).toBe('Your report')
  })

  it('says it is grading while the report is on its way', () => {
    const { text } = render({ status: 'grading' })
    expect(text).toContain('Grading your answers.')
  })

  it('says there is no report for an interview the server does not know', () => {
    const { text } = render({ status: 'not_found' })
    expect(text).toContain('There is no report for this interview.')
  })
})
