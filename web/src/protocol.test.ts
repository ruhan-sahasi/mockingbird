import { describe, expect, it } from 'vitest'
import { parseServerMessage } from './protocol'

describe('parseServerMessage', () => {
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
