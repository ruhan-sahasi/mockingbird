// The app's three pages, picked by the location hash (route.ts).
//
// The interview page opens only after the Start button, which is where consent is given. A
// reload, a typed #/interview or the Forward button lands on the first page instead, so the mic
// never turns on without a fresh Start.

import { useEffect, useRef, useState, useSyncExternalStore } from 'react'
import type { LimitReason } from './api'
import { isPhone } from './device'
import Interview from './pages/Interview'
import Landing from './pages/Landing'
import Report from './pages/Report'
import { INTERVIEW_HASH, LANDING_HASH, parseRoute, reportHash } from './route'

function subscribe(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

function currentHash(): string {
  return window.location.hash
}

export default function App() {
  const route = parseRoute(useSyncExternalStore(subscribe, currentHash))
  const [started, setStarted] = useState(false)
  const [limit, setLimit] = useState<LimitReason | null>(null)
  const [phone] = useState(() =>
    isPhone(navigator.userAgent, navigator.maxTouchPoints, window.innerWidth),
  )

  // Leaving the interview page uses up the Start that opened it.
  const lastPage = useRef(route.page)
  useEffect(() => {
    if (lastPage.current === 'interview' && route.page !== 'interview') setStarted(false)
    lastPage.current = route.page
  }, [route.page])

  const blocked = route.page === 'interview' && !started
  useEffect(() => {
    if (blocked) window.location.replace(LANDING_HASH)
  }, [blocked])

  if (route.page === 'report') return <Report key={route.id} id={route.id} />
  if (route.page === 'interview' && started) {
    return (
      <Interview
        onLimit={(reason) => {
          setLimit(reason)
          window.location.replace(LANDING_HASH)
        }}
        onEnded={(id) => window.location.replace(reportHash(id))}
      />
    )
  }
  return (
    <Landing
      phone={phone}
      limit={limit}
      onStart={() => {
        setStarted(true)
        window.location.hash = INTERVIEW_HASH
      }}
    />
  )
}
