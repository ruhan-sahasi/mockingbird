// The interviewer's audio worklet. The page posts it 16 kHz int16 frames from the server; it
// resamples them to the device's rate into a ring that holds 2 s, and the speakers read from the
// ring. A flush empties the ring at once, which is how the interviewer stops talking the moment
// the candidate talks over it.
//
// It also tells the page when the interviewer starts and stops speaking, for the indicator. The
// server streams silence between lines, so "speaking" means the audio is loud enough, not that
// there is audio at all.

import { int16ToFloat } from './pcm'
import { LinearResampler } from './resample'
import { FloatRing } from './ring'

// What the worklet scope provides. TypeScript's DOM library does not describe that scope, so
// these are declared for this file only.
declare const sampleRate: number
declare class AudioWorkletProcessor {
  readonly port: MessagePort
}
declare function registerProcessor(name: string, processor: typeof AudioWorkletProcessor): void

/** What the page posts to the worklet. */
export type PlaybackCommand = { type: 'push'; pcm: Int16Array } | { type: 'flush' }
/** What the worklet posts to the page. */
export type PlaybackEvent = { type: 'speaking'; speaking: boolean }

const WIRE_RATE = 16000
const RING_SECONDS = 2
// A block is speech when its RMS is at least this, about -40 dBFS. Silence from the server is
// zeros, and quiet room tone in a synthesised voice sits well below it.
const SPEAKING_RMS = 0.01
// Speech has short gaps between words. It counts as stopped only after this long below the level.
const QUIET_BEFORE_STOPPED_S = 0.4

class PlaybackProcessor extends AudioWorkletProcessor {
  private readonly ring = new FloatRing(RING_SECONDS * sampleRate)
  private resampler = new LinearResampler(WIRE_RATE, sampleRate)
  private speaking = false
  private quietSamples = 0

  constructor() {
    super()
    this.port.onmessage = (event: MessageEvent<PlaybackCommand>) => {
      const command = event.data
      if (command.type === 'push') {
        // When the ring is full the newest audio is dropped (ring.ts). With 2 s of room that
        // happens only if the server sends audio far faster than it plays.
        this.ring.write(this.resampler.push(int16ToFloat(command.pcm)))
      } else if (command.type === 'flush') {
        this.ring.clear()
        // A fresh resampler, so the sample it held back from the dropped audio is not played.
        this.resampler = new LinearResampler(WIRE_RATE, sampleRate)
        this.setSpeaking(false)
      }
    }
  }

  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const channels = outputs[0]
    if (channels === undefined || channels.length === 0) return true
    // An empty ring reads as zeros, which is silence.
    this.ring.read(channels[0])
    for (let c = 1; c < channels.length; c++) channels[c].set(channels[0])
    this.track(channels[0])
    return true // keep running
  }

  private track(block: Float32Array): void {
    let sum = 0
    for (let i = 0; i < block.length; i++) sum += block[i] * block[i]
    if (Math.sqrt(sum / block.length) >= SPEAKING_RMS) {
      this.quietSamples = 0
      this.setSpeaking(true)
    } else {
      this.quietSamples += block.length
      if (this.quietSamples >= QUIET_BEFORE_STOPPED_S * sampleRate) this.setSpeaking(false)
    }
  }

  private setSpeaking(speaking: boolean): void {
    if (speaking === this.speaking) return
    this.speaking = speaking
    this.port.postMessage({ type: 'speaking', speaking } satisfies PlaybackEvent)
  }
}

registerProcessor('playback', PlaybackProcessor)
