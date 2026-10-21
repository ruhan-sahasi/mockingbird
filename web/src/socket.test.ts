import { describe, expect, it, vi } from 'vitest'
import { sendFrame, type FrameSocket } from './socket'

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
