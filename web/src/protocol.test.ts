import { readFileSync } from 'node:fs'
import { describe, expect, it } from 'vitest'
import { parseServerMessage, timingMessage } from './protocol'

// What the relay really sends, as the server's tests check it (relay.py's FLUSH, PING and the
// ended frame of its hang-up).
const WIRE = JSON.parse(
  readFileSync(new URL('../../server/tests/fixtures/wire_protocol.json', import.meta.url), 'utf8'),
) as {
  server_to_browser: Record<string, object>
  browser_to_server: { timing: object }
  ended_reasons: string[]
}

describe('parseServerMessage', () => {
  it.each(Object.entries(WIRE.server_to_browser))(
    'parses the %s frame the server sends',
    (type, frame) => {
      expect(parseServerMessage(JSON.stringify(frame))).toEqual(frame)
      expect(parseServerMessage(JSON.stringify(frame))?.type).toBe(type)
    },
  )

  it.each(WIRE.ended_reasons)('parses ended with the server reason %s', (reason) => {
    expect(parseServerMessage(JSON.stringify({ type: 'ended', reason }))).toEqual({
      type: 'ended',
      reason,
    })
  })

  it('parses flush and ping', () => {
    expect(parseServerMessage('{"type":"flush"}')).toEqual({ type: 'flush' })
    expect(parseServerMessage('{"type": "ping"}')).toEqual({ type: 'ping' })
  })

  it.each(['cap', 'browser_closed', 'vapi_closed', 'plan_done', 'error'])(
    'parses ended with the reason %s',
    (reason) => {
      expect(parseServerMessage(JSON.stringify({ type: 'ended', reason }))).toEqual({
        type: 'ended',
        reason,
      })
    },
  )

  it('parses error', () => {
    expect(parseServerMessage('{"type":"error","message":"the mic stopped"}')).toEqual({
      type: 'error',
      message: 'the mic stopped',
    })
  })

  it('keeps only the fields of the message type', () => {
    const msg = parseServerMessage('{"type":"flush","extra":1}')
    expect(msg).toStrictEqual({ type: 'flush' })
  })

  it.each([
    ['empty text', ''],
    ['not JSON', 'hello'],
    ['truncated JSON', '{"type":"flu'],
    ['null', 'null'],
    ['a number', '42'],
    ['a string', '"flush"'],
    ['an array', '[{"type":"flush"}]'],
    ['an empty object', '{}'],
    ['an unknown type', '{"type":"explode"}'],
    ['a type that is not a string', '{"type":1}'],
    ['ended without a reason', '{"type":"ended"}'],
    ['ended with a reason that is not a string', '{"type":"ended","reason":5}'],
    ['error without a message', '{"type":"error"}'],
    ['error with a message that is not a string', '{"type":"error","message":null}'],
  ])('gives null for %s', (_name, text) => {
    expect(parseServerMessage(text)).toBeNull()
  })
})

describe('timingMessage', () => {
  it('builds the timing frame the server reads', () => {
    expect(JSON.parse(timingMessage({ stop_to_audible_ms: 1420 }))).toEqual(
      WIRE.browser_to_server.timing,
    )
  })

  it('carries both numbers when both were measured', () => {
    expect(
      JSON.parse(timingMessage({ stop_to_audible_ms: 1420, interrupt_to_silence_ms: 180 })),
    ).toEqual({ type: 'timing', stop_to_audible_ms: 1420, interrupt_to_silence_ms: 180 })
  })

  it('carries how far into the reply the candidate talked over it', () => {
    expect(timingMessage({ talk_over_ms: 920 })).toBe('{"type":"timing","talk_over_ms":920}')
  })

  it('leaves out a number that was not measured', () => {
    expect(timingMessage({ interrupt_to_silence_ms: 180 })).toBe(
      '{"type":"timing","interrupt_to_silence_ms":180}',
    )
    expect(timingMessage({ stop_to_audible_ms: 0, interrupt_to_silence_ms: undefined })).toBe(
      '{"type":"timing","stop_to_audible_ms":0}',
    )
  })
})
