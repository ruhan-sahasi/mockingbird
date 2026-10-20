// Streaming linear-interpolation resampler, for turning the device's sample rate into 16 kHz
// and back. Audio arrives in chunks, so it carries its place between calls: a stream pushed in
// pieces comes out the same as the same stream pushed in one go.

/**
 * Positions are whole numbers in units of 1/outRate of an input sample, so one input sample is
 * `outRate` units and one output step is `inRate` units. With integer arithmetic the position
 * never drifts however long the stream runs, and chunk edges can not change which samples come
 * out. (Sample rates are whole numbers of Hz, so nothing is lost by requiring that.)
 *
 * An output sample is interpolated between two neighbouring input samples, so the latest input
 * sample is held back until the next chunk brings its neighbour. The stream therefore runs
 * under one input sample behind.
 */
export class LinearResampler {
  private readonly inRate: number
  private readonly outRate: number
  // Where the next output sample falls, counted from the first sample of the next chunk. It goes
  // down to -outRate, which is the previous chunk's last sample.
  private pos = 0
  // The previous chunk's last sample, for an output that falls between it and the next chunk.
  private last = 0

  constructor(inRate: number, outRate: number) {
    for (const rate of [inRate, outRate]) {
      if (!Number.isSafeInteger(rate) || rate <= 0) {
        throw new RangeError(`a sample rate must be a positive whole number, got ${rate}`)
      }
    }
    this.inRate = inRate
    this.outRate = outRate
  }

  push(x: Float32Array): Float32Array {
    const n = x.length
    if (n === 0) return new Float32Array(0)

    const { inRate, outRate } = this
    // An output at position p needs input samples floor(p) and floor(p) + 1 from this chunk or
    // the held-back one, so it can be made now only while p < (n - 1) * outRate.
    const limit = (n - 1) * outRate
    const count = this.pos < limit ? Math.ceil((limit - this.pos) / inRate) : 0

    const out = new Float32Array(count)
    let p = this.pos
    for (let k = 0; k < count; k++, p += inRate) {
      const i = Math.floor(p / outRate)
      const frac = (p - i * outRate) / outRate
      const a = i < 0 ? this.last : x[i]
      out[k] = a + (x[i + 1] - a) * frac
    }

    this.pos = p - n * outRate
    this.last = x[n - 1]
    return out
  }
}
