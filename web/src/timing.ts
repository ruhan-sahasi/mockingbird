// The browser's two latency numbers, both on one clock (performance.now) so neither depends on
// the server's clock or the network:
//
// - stop to audible: from the candidate's end of speech to the first sample of the interviewer's
//   reply leaving the speakers. This is the headline number. The VAD reports a stop only after a
//   stretch of quiet, so the stop is backdated by that stretch. The worklet hears the reply start
//   before the speakers play it, so the output latency is added.
// - interrupt to silence: from the candidate starting to talk over the interviewer to the
//   interviewer going quiet, once the server has told the page to flush the reply.
//
// The VAD's events (audio/vad.ts) and the player's speaking events come in, and each number
// comes out once, from the call that completes it. The server ties it to the reply it is
// speaking (tracing.py).

import type { VadEvent } from './audio/vad'
import type { Timing } from './protocol'

export type TurnTimerClock = {
  /** The time in ms. */
  now(): number
  /** How long a sample takes from the player to the speakers, in ms. */
  outputLatencyMs(): number
}

export class TurnTimer {
  private readonly clock: TurnTimerClock
  private audible = false // whether the interviewer is speaking, as the player last said
  private stoppedAt: number | null = null // the candidate's latest stop not yet answered
  private interruptAt: number | null = null // when the candidate talked over the interviewer
  private flushPending = false // the server flushed the reply, and it has not gone quiet yet

  constructor(clock: TurnTimerClock) {
    this.clock = clock
  }

  /** The candidate started or stopped speaking. A later stop replaces an earlier one. */
  vad(e: VadEvent): void {
    if (!e.speaking) this.stoppedAt = this.clock.now() - (e.quietMs ?? 0)
    else if (this.audible) this.interruptAt = this.clock.now()
  }

  /**
   * The interviewer started or stopped speaking. Starting gives stop to audible when a stop is
   * pending. Stopping after a flush gives interrupt to silence when the candidate had talked over
   * it. Gives null otherwise.
   */
  interviewerAudible(audible: boolean): Timing | null {
    this.audible = audible
    if (audible) {
      // A flush that came while the interviewer was quiet has nothing left to silence.
      this.flushPending = false
      if (this.stoppedAt === null) return null
      const audibleAt = this.clock.now() + this.clock.outputLatencyMs()
      const timing = { stop_to_audible_ms: audibleAt - this.stoppedAt }
      this.stoppedAt = null
      return timing
    }
    const { interruptAt, flushPending } = this
    // Gone quiet either way, so a cut-in the server let pass is forgotten, and a later flush is
    // never timed from it.
    this.interruptAt = null
    this.flushPending = false
    if (!flushPending || interruptAt === null) return null
    return { interrupt_to_silence_ms: this.clock.now() - interruptAt }
  }

  /** The server told the page to flush the reply, because the candidate talked over it. */
  flushed(): void {
    this.flushPending = true
  }
}
