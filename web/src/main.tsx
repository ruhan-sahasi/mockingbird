import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
// The fonts are bundled with the app, not fetched from a font service, so a visit sends nothing to
// a third party. Plex Sans sets the page; Plex Serif italic sets only the candidate's own words.
import '@fontsource-variable/ibm-plex-sans'
import '@fontsource/ibm-plex-serif/400-italic.css'
import './styles.css'
import App from './App.tsx'

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
