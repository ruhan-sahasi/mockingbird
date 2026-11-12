// The mic's audio worklet. It runs on the audio thread at the device's sample rate, resamples the
// mic to 16 kHz and posts one 320-sample Int16Array frame (20 ms, 640 bytes) to the page at a
// time. The page sends each frame to the server as it comes, so nothing waits here.
//
// It posts frames whatever the mic is doing, because the server ends the call once 2 s pass
// without one (relay.py's browser_timeout_s). A muted mic gives zeros, and an input with no
// channels (no audio has reached the node) is read as a block of zeros.
//
// Each frame also goes through the VAD (vad.ts), here on the audio thread, so the moment the
// candidate stops speaking is judged on the mic's own 20 ms clock. When it says the candidate
// started or stopped, that goes to the page as a message of its own, after the frame.

import { FRAME_SAMPLES, floatToInt16 } from './pcm'
import { LinearResampler } from './resample'
import { EnergyVad, type VadEvent } from './vad'

// What the worklet scope provides. TypeScript's DOM library does not describe that scope, so
// these are declared for this file only.
declare const sampleRate: number
declare class AudioWorkletProcessor {
  readonly port: MessagePort
}
declare function registerProcessor(name: string, processor: typeof AudioWorkletProcessor): void

/** What the worklet posts to the page. A frame's buffer is handed over, never shared. */
export type CaptureMessage =
  | { type: 'frame'; pcm: Int16Array<ArrayBuffer> }
  | ({ type: 'vad' } & VadEvent)

const WIRE_RATE = 16000
// Every browser renders in blocks of 128 samples.
const RENDER_QUANTUM = 128
const SILENCE = new Float32Array(RENDER_QUANTUM)

class CaptureProcessor extends AudioWorkletProcessor {
  private readonly resampler = new LinearResampler(sampleRate, WIRE_RATE)
  private readonly frame = new Float32Array(FRAME_SAMPLES)
  private filled = 0
  private readonly vad = new EnergyVad()

  process(inputs: Float32Array[][]): boolean {
    // The node is set to one input channel, so the browser mixes a stereo mic down to it.
    const block = inputs[0]?.[0] ?? SILENCE
    const resampled = this.resampler.push(block)
    let at = 0
    while (at < resampled.length) {
      const n = Math.min(resampled.length - at, FRAME_SAMPLES - this.filled)
      this.frame.set(resampled.subarray(at, at + n), this.filled)
      this.filled += n
      at += n
      if (this.filled === FRAME_SAMPLES) {
        const frame: CaptureMessage = { type: 'frame', pcm: floatToInt16(this.frame) }
        this.port.postMessage(frame, [frame.pcm.buffer])
        const change = this.vad.push(this.frame)
        if (change !== null) {
          this.port.postMessage({ type: 'vad', ...change } satisfies CaptureMessage)
        }
        this.filled = 0
      }
    }
    return true // keep running
  }
}

registerProcessor('capture', CaptureProcessor)
