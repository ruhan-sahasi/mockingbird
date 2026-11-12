// The mic: getUserMedia into the capture worklet, which hands back 16 kHz int16 frames, and says
// when the candidate starts and stops speaking.

import type { CaptureMessage } from './capture.worklet'
import { openWorkletContext } from './context'
import captureUrl from './capture.worklet.ts?worker&url'
import type { VadEvent } from './vad'

export type Mic = { stop(): void }

/**
 * Ask for the mic and start capturing. `onFrame` gets one 320-sample frame every 20 ms from now
 * until `stop()`, silent ones included, and `onVad` hears each time the candidate starts or stops
 * speaking (vad.ts). Rejects with getUserMedia's own error when the mic is refused or missing (a
 * DOMException named NotAllowedError, NotFoundError and so on), and with AudioBlockedError when
 * the browser will not start audio.
 */
export async function startMic(
  onFrame: (pcm: Int16Array<ArrayBuffer>) => void,
  onVad?: (e: VadEvent) => void,
): Promise<Mic> {
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

  let source: MediaStreamAudioSourceNode
  let node: AudioWorkletNode
  try {
    source = ctx.createMediaStreamSource(stream)
    node = new AudioWorkletNode(ctx, 'capture', {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      channelCount: 1,
      channelCountMode: 'explicit',
    })
    node.port.onmessage = (event: MessageEvent<CaptureMessage>) => {
      const message = event.data
      if (message.type === 'frame') onFrame(message.pcm)
      else {
        const { speaking, loudMs, quietMs } = message
        onVad?.({ speaking, loudMs, quietMs })
      }
    }
    // If the worklet throws, frames stop, and the server ends the call 2 s later. Say why here.
    node.onprocessorerror = () => console.error('the mic worklet stopped with an error')
    source.connect(node)
    // The worklet writes nothing to its output. It is connected so the context keeps running it.
    node.connect(ctx.destination)
  } catch (error) {
    // Let go of the mic, or the browser shows it recording after the page has said it failed.
    stopTracks()
    void ctx.close()
    throw error
  }

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
