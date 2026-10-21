// What the relay sends the browser as text frames (server/mockingbird/relay.py). Binary frames
// are audio and never reach this parser. The only text the browser sends back is
// {"type": "end"}.

export type ServerMessage =
  // The user talked over the interviewer: drop the audio still queued to play.
  | { type: 'flush' }
  // A keepalive. It needs no reply.
  | { type: 'ping' }
  // The call is over. The server sends "cap", "browser_closed", "vapi_closed", "plan_done" or
  // "error", but any string is passed through so a new reason can not make the end go unnoticed.
  | { type: 'ended'; reason: string }
  | { type: 'error'; message: string }

/** Parse one text frame from the server. Anything that is not a known message gives null. */
export function parseServerMessage(s: string): ServerMessage | null {
  let data: unknown
  try {
    data = JSON.parse(s)
  } catch {
    return null
  }
  if (typeof data !== 'object' || data === null || Array.isArray(data)) return null

  const { type, reason, message } = data as Record<string, unknown>
  switch (type) {
    case 'flush':
      return { type: 'flush' }
    case 'ping':
      return { type: 'ping' }
    case 'ended':
      return typeof reason === 'string' ? { type: 'ended', reason } : null
    case 'error':
      return typeof message === 'string' ? { type: 'error', message } : null
    default:
      return null
  }
}
