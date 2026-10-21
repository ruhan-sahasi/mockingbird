import { afterEach, describe, expect, it, vi } from 'vitest'
import { openSocket, sendFrame, type FrameSocket } from './socket'

function frame(): Int16Array<ArrayBuffer> {
  return new Int16Array(320)
}

function fakeSocket(readyState: FrameSocket['readyState']) {
  return { readyState, send: vi.fn() }
}

describe('sendFrame', () => {
  it('sends the frame as it is while the socket is open', () => {
    const socket = fakeSocket(WebSocket.OPEN)
    const pcm = frame()
    expect(sendFrame(socket, pcm)).toBe(true)
    expect(socket.send).toHaveBeenCalledTimes(1)
    expect(socket.send.mock.calls[0][0]).toBe(pcm)
  })

  it.each([
    ['connecting', WebSocket.CONNECTING],
    ['closing', WebSocket.CLOSING],
    ['closed', WebSocket.CLOSED],
  ])('drops the frame while the socket is %s', (_name, state) => {
    const socket = fakeSocket(state)
    expect(sendFrame(socket, frame())).toBe(false)
    expect(socket.send).not.toHaveBeenCalled()
  })

  it('drops the frame before there is a socket', () => {
    expect(sendFrame(null, frame())).toBe(false)
  })

  it('never sends a frame from before the socket opened, once it has', () => {
    const socket = fakeSocket(WebSocket.CONNECTING)
    sendFrame(socket, frame())
    sendFrame(socket, frame())
    socket.readyState = WebSocket.OPEN
    const live = frame()
    sendFrame(socket, live)
    expect(socket.send).toHaveBeenCalledTimes(1)
    expect(socket.send.mock.calls[0][0]).toBe(live)
  })
})

describe('openSocket', () => {
  class FakeWebSocket {
    static readonly OPEN = 1
    static made: FakeWebSocket[] = []
    binaryType = 'blob'
    onmessage: ((event: { data: unknown }) => void) | null = null
    onclose: ((event: { code: number }) => void) | null = null
    readonly url: string
    constructor(url: string) {
      this.url = url
      FakeWebSocket.made.push(this)
    }
  }

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('takes binary frames as ArrayBuffers and hands on each frame and the close code', () => {
    FakeWebSocket.made = []
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const messages: unknown[] = []
    const codes: number[] = []

    const socket = openSocket('wss://site.test/ws/interview/1?token=t', {
      onMessage: (data) => messages.push(data),
      onClose: (code) => codes.push(code),
    })

    const [made] = FakeWebSocket.made
    expect(socket).toBe(made)
    expect(made.url).toBe('wss://site.test/ws/interview/1?token=t')
    expect(made.binaryType).toBe('arraybuffer') // the session reads audio as an ArrayBuffer
    const audio = new ArrayBuffer(640)
    made.onmessage!({ data: audio })
    made.onmessage!({ data: '{"type":"flush"}' })
    made.onclose!({ code: 4401 })
    expect(messages).toEqual([audio, '{"type":"flush"}'])
    expect(codes).toEqual([4401])
  })
})
