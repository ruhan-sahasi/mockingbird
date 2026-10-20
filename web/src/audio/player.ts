// The speakers: 16 kHz int16 frames from the server into the playback worklet.

import { openWorkletContext } from './context'
import type { PlaybackCommand, PlaybackEvent } from './playback.worklet'
import playbackUrl from './playback.worklet.ts?worker&url'

export type Player = {
  /** Queue one frame of 16 kHz s16le mono audio to play after what is queued already. */
  push(pcm: Int16Array): void
  /** Drop everything queued, at once. */
  flush(): void
  close(): void
}

/**
 * Open the speakers. `onSpeaking` hears when the interviewer's audio starts and stops being
 * speech. Rejects with AudioBlockedError when the browser will not start audio.
 */
export async function createPlayer(onSpeaking?: (speaking: boolean) => void): Promise<Player> {
  const ctx = await openWorkletContext(playbackUrl)
  const node = new AudioWorkletNode(ctx, 'playback', {
    numberOfInputs: 0,
    numberOfOutputs: 1,
    outputChannelCount: [1],
  })
  node.port.onmessage = (event: MessageEvent<PlaybackEvent>) => {
    if (event.data.type === 'speaking') onSpeaking?.(event.data.speaking)
  }
  node.onprocessorerror = () => console.error('the playback worklet stopped with an error')
  node.connect(ctx.destination)

  const send = (command: PlaybackCommand) => node.port.postMessage(command)
  return {
    push(pcm) {
      send({ type: 'push', pcm })
    },
    flush() {
      send({ type: 'flush' })
    },
    close() {
      node.port.onmessage = null
      node.port.close()
      node.disconnect()
      void ctx.close()
    },
  }
}
