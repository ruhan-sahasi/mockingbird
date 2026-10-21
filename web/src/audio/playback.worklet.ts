// The interviewer's audio worklet. The page posts it 16 kHz int16 frames from the server; it
// resamples them to the device's rate into a ring that holds 2 s, and the speakers read from the
// ring. A flush empties the ring at once, which is how the interviewer stops talking the moment
// the candidate talks over it.
//
// The ring is also the jitter buffer. Vapi's audio arrives in bursts, not paced: in the recording
// (server/tests/fixtures/vapi/ws_binary_frames_first20.json) 2 to 4 frames came together with
// gaps of up to 0.14 s, and played as they came, the ring ran dry in the middle of the opening
// line. So the speakers stay silent until PREROLL_S of audio is queued, and from then on the
// queue absorbs the gaps. Vapi streams silence between lines, so the queue stays about that full
// for the whole call, and every reply starts that much later. It waits for a full pre-roll again
// after a flush (the next reply) and after the ring runs dry (a stall longer than the queue),
// rather than stuttering through frames as they trickle in.
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
// How much audio must be queued before the speakers start. Larger rides out bigger gaps, and adds
// its own length to the delay before every reply is heard.
const PREROLL_S = 0.1

class PlaybackProcessor extends AudioWorkletProcessor {
  private readonly ring = new FloatRing(RING_SECONDS * sampleRate)
  private resampler = new LinearResampler(WIRE_RATE, sampleRate)
  private readonly preroll = Math.round(PREROLL_S * sampleRate)
  private playing = false // false while the pre-roll fills
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
        this.playing = false // the next reply gets its pre-roll too
        this.setSpeaking(false)
      }
    }
  }

  process(_inputs: Float32Array[][], outputs: Float32Array[][]): boolean {
    const channels = outputs[0]
    if (channels === undefined || channels.length === 0) return true
    const out = channels[0]
    if (!this.playing && this.ring.available >= this.preroll) this.playing = true
    if (this.playing) {
      // An empty ring reads as zeros, which is silence. Running dry starts a new pre-roll.
      if (this.ring.read(out) < out.length) this.playing = false
    } else {
      out.fill(0)
    }
    for (let c = 1; c < channels.length; c++) channels[c].set(out)
    this.track(out)
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
