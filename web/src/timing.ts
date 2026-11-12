// The browser's two latency numbers, both on one clock (performance.now) so neither depends on
// the server's clock or the network:
//
// - stop to audible: from the candidate's end of speech to the first sample of the interviewer's
//   reply leaving the speakers. This is the headline number. The VAD reports a stop only after a
//   stretch of quiet, so the stop is backdated by that stretch. The worklet hears the reply start
//   before the speakers play it, so the output latency is added.
// - interrupt to silence: from the candidate starting to talk over the interviewer to the
//   interviewer going quiet, once the server has told the page to flush the reply. The VAD
//   reports speech only after a few loud frames, so the cut-in is backdated by them. The player
//   goes quiet while the speakers are still playing what it handed over, so the output latency
//   is added here too.
//
// Only the end of an answer starts a stop to audible measurement: the candidate's last stop with
// no speech after it, heard while the interviewer is quiet. A stop while the interviewer is
// speaking ends a cough or an "mm-hmm" over the reply, not an answer. Speech after a stop means
// the stop was a pause mid-answer, so if the interviewer then cuts in, nothing is timed.
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
  private stoppedAt: number | null = null // the end of the candidate's answer, not yet answered
  private interruptAt: number | null = null // when the candidate first talked over the interviewer
  private flushPending = false // the server flushed the reply, and it has not gone quiet yet

  constructor(clock: TurnTimerClock) {
    this.clock = clock
  }

  /**
   * The candidate started or stopped speaking. A stop counts only while the interviewer is quiet.
   * Speaking drops any pending stop, and while the interviewer is heard it is a cut-in, of which
   * the first one counts until the interviewer goes quiet.
   */
  vad(e: VadEvent): void {
    const at = this.clock.now()
    if (!e.speaking) {
      if (!this.audible) this.stoppedAt = at - (e.quietMs ?? 0)
      return
    }
    this.stoppedAt = null
    if (this.audible && this.interruptAt === null) this.interruptAt = at - (e.loudMs ?? 0)
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
    const silentAt = this.clock.now() + this.clock.outputLatencyMs()
    return { interrupt_to_silence_ms: silentAt - interruptAt }
  }

  /** The server told the page to flush the reply, because the candidate talked over it. */
  flushed(): void {
    this.flushPending = true
  }
}
