// Mic audio onto the interview socket. The Interview page calls this for every frame the mic
// gives, from before the socket exists until after it closes.

/** The part of a WebSocket that sending a frame uses. */
export type FrameSocket = Pick<WebSocket, 'readyState' | 'send'>

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
