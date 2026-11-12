// When the candidate starts and stops speaking, judged by loudness alone. The capture worklet runs
// it on every 20 ms frame of the mic, and the page times the interview's turns from what it says
// (timing.ts).
//
// Speaking starts after a few loud frames in a row, so a click does not count. It stops only
// after a stretch of quiet, so the gaps between words do not count either. Either way the
// change happened before it is reported: the loud frames or the quiet stretch have already
// passed. So each event says how long that was, and the page backdates the change by that much.

/**
 * A change in whether the candidate is speaking. A start carries `loudMs` and a stop carries
 * `quietMs`: how long ago, in ms, the change really happened.
 */
export type VadEvent = { speaking: boolean; loudMs?: number; quietMs?: number }

export type VadOptions = {
  /** A frame this loud or louder, as RMS on the float scale of -1 to 1, is speech. */
  speechRms?: number
  /** How many speech frames in a row start speaking. */
  startFrames?: number
  /** How long below speechRms stops speaking. */
  quietMs?: number
  /** How long one frame is. */
  frameMs?: number
}

export class EnergyVad {
  private readonly speechRms: number
  private readonly startFrames: number
  private readonly quietMs: number
  private readonly frameMs: number
  private speaking = false
  private loudFrames = 0 // speech frames in a row
  private quietFor = 0 // ms below speechRms since the last speech frame, while speaking

  constructor({ speechRms = 0.02, startFrames = 3, quietMs = 300, frameMs = 20 }: VadOptions = {}) {
    this.speechRms = speechRms
    this.startFrames = startFrames
    this.quietMs = quietMs
    this.frameMs = frameMs
  }

  /** Take the next frame. Gives an event only when speaking starts or stops, and null otherwise. */
  push(frame: Float32Array): VadEvent | null {
    if (rms(frame) >= this.speechRms) {
      this.loudFrames++
      this.quietFor = 0
      if (this.speaking || this.loudFrames < this.startFrames) return null
      this.speaking = true
      return { speaking: true, loudMs: this.startFrames * this.frameMs }
    }
    this.loudFrames = 0
    if (!this.speaking) return null
    this.quietFor += this.frameMs
    if (this.quietFor < this.quietMs) return null
    this.speaking = false
    const quietMs = this.quietFor
    this.quietFor = 0
    return { speaking: false, quietMs }
  }
}

function rms(frame: Float32Array): number {
  if (frame.length === 0) return 0
  let sum = 0
  for (let i = 0; i < frame.length; i++) sum += frame[i] * frame[i]
  return Math.sqrt(sum / frame.length)
}
