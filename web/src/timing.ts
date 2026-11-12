// The browser's numbers for each turn, all on one clock (performance.now) so none depends on the
// server's clock or the network:
//
// - stop to audible: from the candidate's end of speech to the first sample of the interviewer's
//   reply leaving the speakers. This is the headline number. The VAD reports a stop only after a
//   stretch of quiet, so the stop is backdated by that stretch. The worklet hears the reply start
//   before the speakers play it, so the output latency is added.
// - interrupt to silence: from the candidate starting to talk over the interviewer to the
//   interviewer going quiet, once the server has told the page to flush the reply. The VAD
//   reports speech only after a few loud frames, so the cut-in is backdated by them. The player
//   goes quiet while the speakers are still playing what it handed over, so the output latency
//   is added here too. The cut-in timed is the one the flush answered: talk over the reply that
//   ended with no flush, such as an "mm-hmm" or a cough Vapi let pass, is forgotten when it ends,
//   so a real cut-in seconds later is not timed from it.
// - talk over: from the first sample of the reply leaving the speakers to the candidate starting
//   to talk over it, given when the server flushes the reply for that cut-in. The server counts
//   the reply as a cut-in when this is 1.5 s or less. It runs from what the candidate heard, so
//   it does not move with Vapi's interruption settings or the player's pre-roll, as the server's
//   own stand-in does. A reply's start is its first audible sample since the candidate last
//   spoke while the interviewer was quiet, so a pause between two of its sentences does not
//   restart it.
//
// Only the end of an answer starts a stop to audible measurement: the candidate's last stop with
// no speech after it, heard while the interviewer is quiet. A stop while the interviewer is
// speaking ends a cough or an "mm-hmm" over the reply, not an answer. Speech after a stop means
// the stop was a pause mid-answer, so if the interviewer then cuts in, nothing is timed.
//
// The VAD's events (audio/vad.ts), the player's speaking events and the server's flush come in,
// and each number comes out once, from the call that completes it. The server ties it to the reply it is
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
  private interruptAt: number | null = null // when the candidate started talking over the reply
  private flushPending = false // the server flushed the reply, and it has not gone quiet yet
  private replyAudibleAt: number | null = null // when the reply being heard first left the speakers

  constructor(clock: TurnTimerClock) {
    this.clock = clock
  }

  /**
   * The candidate started or stopped speaking. A stop counts only while the interviewer is quiet.
   * Speaking drops any pending stop, and while the interviewer is heard it is a cut-in. A cut-in
   * that stops with no flush pending was let pass, so it is dropped. Otherwise the first one
   * counts until the interviewer goes quiet.
   */
  vad(e: VadEvent): void {
    const at = this.clock.now()
    // The candidate's turn: whatever the interviewer says next is a new reply.
    if (!this.audible) this.replyAudibleAt = null
    if (!e.speaking) {
      if (!this.audible) this.stoppedAt = at - (e.quietMs ?? 0)
      if (!this.flushPending) this.interruptAt = null
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
      const audibleAt = this.clock.now() + this.clock.outputLatencyMs()
      if (this.replyAudibleAt === null) this.replyAudibleAt = audibleAt
      if (this.stoppedAt === null) return null
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

  /**
   * The server told the page to flush the reply, because the candidate talked over it. Gives
   * talk over when the cut-in was heard here, and null otherwise. A cut-in heard as the reply
   * started can be backdated to before its first sample, and counts as 0 ms.
   */
  flushed(): Timing | null {
    this.flushPending = true
    if (this.interruptAt === null || this.replyAudibleAt === null) return null
    return { talk_over_ms: Math.max(0, this.interruptAt - this.replyAudibleAt) }
  }
}
