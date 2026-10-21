// A fixed-size queue of float samples, for the player to hold audio between the socket and the
// speakers. It is not thread-safe: the write and the read must run on the same thread, as they
// do in an audio worklet (its message handler and its process callback share a thread).

export class FloatRing {
  private readonly buffer: Float32Array
  private head = 0 // where the next read starts
  private count = 0

  constructor(capacity: number) {
    if (!Number.isSafeInteger(capacity) || capacity <= 0) {
      throw new RangeError(`capacity must be a positive whole number, got ${capacity}`)
    }
    this.buffer = new Float32Array(capacity)
  }

  /** Samples written and not yet read. */
  get available(): number {
    return this.count
  }

  /**
   * Add samples at the back and return how many were kept. When the ring is full the samples
   * that do not fit are dropped, so audio already queued is never overwritten.
   */
  write(x: Float32Array): number {
    const capacity = this.buffer.length
    const n = Math.min(x.length, capacity - this.count)
    const start = (this.head + this.count) % capacity
    const firstRun = Math.min(n, capacity - start)
    this.buffer.set(x.subarray(0, firstRun), start)
    this.buffer.set(x.subarray(firstRun, n), 0)
    this.count += n
    return n
  }

  /**
   * Fill `out` from the front. If fewer samples are queued than `out` holds, the rest of `out`
   * is set to zeros (silence). Returns how many real samples were read.
   */
  read(out: Float32Array): number {
    const capacity = this.buffer.length
    const n = Math.min(out.length, this.count)
    const firstRun = Math.min(n, capacity - this.head)
    out.set(this.buffer.subarray(this.head, this.head + firstRun), 0)
    out.set(this.buffer.subarray(0, n - firstRun), firstRun)
    out.fill(0, n)
    this.head = (this.head + n) % capacity
    this.count -= n
    return n
  }

  /** Drop everything queued, at once. */
  clear(): void {
    this.head = 0
    this.count = 0
  }
}
