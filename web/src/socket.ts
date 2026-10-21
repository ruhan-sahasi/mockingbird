// The interview socket: opening it, and putting mic audio onto it. The interview session calls
// sendFrame for every frame the mic gives, from before the socket exists until after it closes.

/** The part of a WebSocket that sending a frame uses. */
export type FrameSocket = Pick<WebSocket, 'readyState' | 'send'>

/** What the interview socket hands on: each frame, and the code it closed with. */
export type SocketHandlers = {
  onMessage(data: string | ArrayBuffer): void
  onClose(code: number): void
}

/**
 * Open the interview socket at `url`. Binary frames (the interviewer's audio) arrive as
 * ArrayBuffers, text frames as strings.
 */
export function openSocket(url: string, handlers: SocketHandlers): WebSocket {
  const socket = new WebSocket(url)
  socket.binaryType = 'arraybuffer'
  socket.onmessage = (event: MessageEvent<string | ArrayBuffer>) => handlers.onMessage(event.data)
  socket.onclose = (event: CloseEvent) => handlers.onClose(event.code)
  return socket
}

/**
 * Send one mic frame now if the socket is open, and drop it otherwise. Nothing is saved up for
 * later: the server wants live audio, and WebSocket.send throws while the socket is still
 * connecting. Gives whether the frame was sent.
 */
export function sendFrame(socket: FrameSocket | null, pcm: Int16Array<ArrayBuffer>): boolean {
  if (socket?.readyState !== WebSocket.OPEN) return false
  socket.send(pcm)
  return true
}
