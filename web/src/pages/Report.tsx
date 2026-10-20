// One interview's report. The page asks for it every 2 s until grading is done, then shows each
// question with a score, a rationale and the candidate's own words for every rubric item.

import { useEffect, useState } from 'react'
import { pollReport, type ItemReport, type QuestionReport, type ReportResult } from '../api'
import { LANDING_HASH } from '../route'

export default function Report({ id }: { id: string }) {
  // null until the server first answers. App keys this page by id, so another report starts over.
  const [result, setResult] = useState<ReportResult | null>(null)
  const [retrying, setRetrying] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    pollReport(id, controller.signal, {
      onGrading: () => {
        setResult({ status: 'grading' })
        setRetrying(false)
      },
      onRetry: () => setRetrying(true),
    }).then(setResult, () => {
      // Aborted: the page has gone.
    })
    return () => controller.abort()
  }, [id])

  return (
    <main className="page report">
      <header className="masthead">
        <a className="wordmark" href={LANDING_HASH}>
          Mockingbird
        </a>
      </header>
      <ReportView result={result} retrying={retrying} />
    </main>
  )
}

const ITEM_NAMES: Record<string, string> = {
  situation: 'Situation',
  task: 'Task',
  action: 'Action',
  result: 'Result',
  specificity: 'Specificity',
  ownership: 'Ownership',
}

const MAX_SCORE = 4

/**
 * The report, or what to say while there is none. `result` is null before the server has
 * answered, and `retrying` says the last request failed.
 */
export function ReportView({
  result,
  retrying = false,
}: {
  result: ReportResult | null
  retrying?: boolean
}) {
  return (
    <>
      <h1 className="report-title">Your report</h1>
      {result === null ? (
        retrying && <p className="notice">{RETRYING}</p>
      ) : result.status === 'graded' ? (
        <>
          <p className="lede">
            Each answer is scored from 1 to 4 on six parts of a strong answer. A score is shown only
            when words you said back it up, and those words are quoted under it.
          </p>
          {result.questions.map((question) => (
            <Question key={question.question_idx} question={question} />
          ))}
        </>
      ) : (
        <div className="notice" role="status">
          <p>{STATUS_SENTENCES[result.status]}</p>
          {result.status === 'grading' && retrying && <p className="hint">{RETRYING}</p>}
        </div>
      )}
    </>
  )
}

const RETRYING = 'The server did not answer the last check. Still trying.'

const STATUS_SENTENCES = {
  grading: 'Grading your answers. This page updates by itself when the report is ready.',
  not_found: 'There is no report for this interview.',
  no_answers: 'No answer was recorded, so there was nothing to grade.',
  grading_failed: 'The grader could not score this interview.',
}

function Question({ question }: { question: QuestionReport }) {
  return (
    <section className="question">
      <p className="question-number">Question {question.question_idx + 1}</p>
      <h2 className="question-text">{question.question_text}</h2>
      <ul className="items">
        {question.items.map((item, i) => (
          <Item key={`${item.item}-${i}`} item={item} />
        ))}
      </ul>
    </section>
  )
}

function Item({ item }: { item: ItemReport }) {
  const score = item.status === 'scored' ? item.score : null
  return (
    <li className="item">
      <div className="item-head">
        <h3 className="item-name">{ITEM_NAMES[item.item] ?? item.item}</h3>
        {score !== null ? (
          <p className="score">
            <span className="score-value">{score}</span>
            <span className="score-of"> of {MAX_SCORE}</span>
            <Meter score={score} />
          </p>
        ) : (
          <p className="score score-missing">Not enough evidence</p>
        )}
      </div>
      <div className="item-body">
        <p className="rationale">{item.rationale}</p>
        {item.quotes.length > 0 && (
          <ul className="quotes" aria-label="What you said">
            {item.quotes.map((quote, i) => (
              <li key={i} className="quote">
                “{quote}”
              </li>
            ))}
          </ul>
        )}
      </div>
    </li>
  )
}

function Meter({ score }: { score: number }) {
  return (
    <span className="meter" aria-hidden="true">
      {Array.from({ length: MAX_SCORE }, (_, i) => (
        <span key={i} className="meter-step" data-filled={i < score} />
      ))}
    </span>
  )
}
