// The speakers: 16 kHz int16 frames from the server into the playback worklet.

import { openWorkletContext } from './context'
import type { PlaybackCommand, PlaybackEvent } from './playback.worklet'
import playbackUrl from './playback.worklet.ts?worker&url'

export type Player = {
  /** Queue one frame of 16 kHz s16le mono audio to play after what is queued already. */
  push(pcm: Int16Array): void
  /** Drop everything queued, at once. */
  flush(): void
  /**
   * How long, in ms, a sample takes from the worklet to the speakers, as the browser estimates
   * it now. Browsers that give no estimate count as 0.
   */
  outputLatencyMs(): number
  close(): void
}

/**
 * Open the speakers. `onSpeaking` hears when the interviewer's audio starts and stops being
 * speech. Rejects with AudioBlockedError when the browser will not start audio.
 */
export async function createPlayer(onSpeaking?: (speaking: boolean) => void): Promise<Player> {
  const ctx = await openWorkletContext(playbackUrl)
  let node: AudioWorkletNode
  try {
    node = new AudioWorkletNode(ctx, 'playback', {
      numberOfInputs: 0,
      numberOfOutputs: 1,
      outputChannelCount: [1],
    })
    node.port.onmessage = (event: MessageEvent<PlaybackEvent>) => {
      if (event.data.type === 'speaking') onSpeaking?.(event.data.speaking)
    }
    node.onprocessorerror = () => console.error('the playback worklet stopped with an error')
    node.connect(ctx.destination)
  } catch (error) {
    void ctx.close() // a context left open keeps the audio device busy
    throw error
  }

  const send = (command: PlaybackCommand) => node.port.postMessage(command)
  return {
    push(pcm) {
      send({ type: 'push', pcm })
    },
    flush() {
      send({ type: 'flush' })
    },
    outputLatencyMs() {
      // outputLatency covers the whole way out, device included, but not every browser gives it.
      // baseLatency is only the context's own share, and better than nothing.
      return (ctx.outputLatency || ctx.baseLatency || 0) * 1000
    },
    close() {
      node.port.onmessage = null
      node.port.close()
      node.disconnect()
      void ctx.close()
    },
  }
}
