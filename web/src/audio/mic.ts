// The mic: getUserMedia into the capture worklet, which hands back 16 kHz int16 frames.

import { openWorkletContext } from './context'
import captureUrl from './capture.worklet.ts?worker&url'

export type Mic = { stop(): void }

/**
 * Ask for the mic and start capturing. `onFrame` gets one 320-sample frame every 20 ms from now
 * until `stop()`, silent ones included. Rejects with getUserMedia's own error when the mic is
 * refused or missing (a DOMException named NotAllowedError, NotFoundError and so on), and with
 * AudioBlockedError when the browser will not start audio.
 */
export async function startMic(onFrame: (pcm: Int16Array<ArrayBuffer>) => void): Promise<Mic> {
  const stream = await navigator.mediaDevices.getUserMedia({
    audio: {
      echoCancellation: true,
      noiseSuppression: true,
      autoGainControl: true,
      channelCount: 1,
    },
  })
  const stopTracks = () => {
    for (const track of stream.getTracks()) track.stop()
  }

  let ctx: AudioContext
  try {
    ctx = await openWorkletContext(captureUrl)
  } catch (error) {
    stopTracks()
    throw error
  }

  const source = ctx.createMediaStreamSource(stream)
  const node = new AudioWorkletNode(ctx, 'capture', {
    numberOfInputs: 1,
    numberOfOutputs: 1,
    outputChannelCount: [1],
    channelCount: 1,
    channelCountMode: 'explicit',
  })
  // A posted frame always arrives on an ArrayBuffer of its own, never a shared one.
  node.port.onmessage = (event: MessageEvent<Int16Array<ArrayBuffer>>) => onFrame(event.data)
  // If the worklet throws, frames stop, and the server ends the call 2 s later. Say why here.
  node.onprocessorerror = () => console.error('the mic worklet stopped with an error')
  source.connect(node)
  // The worklet writes nothing to its output. It is connected so the context keeps running it.
  node.connect(ctx.destination)

  return {
    stop() {
      node.port.onmessage = null
      node.port.close()
      source.disconnect()
      node.disconnect()
      stopTracks()
      void ctx.close()
    },
  }
}
