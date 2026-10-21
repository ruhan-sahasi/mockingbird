// The wire format is raw s16le mono PCM at 16 kHz in 20 ms frames: 320 samples, 640 bytes.
// The audio code works in floats between -1 and 1, so these convert between the two.

export const FRAME_SAMPLES = 320

/** Float samples to int16. Anything outside [-1, 1] is clamped; NaN becomes silence. */
export function floatToInt16(x: Float32Array): Int16Array {
  const out = new Int16Array(x.length)
  for (let i = 0; i < x.length; i++) {
    const v = Math.max(-1, Math.min(1, x[i]))
    // int16 runs from -32768 to 32767, so each side gets its own scale to make 1 and -1 land
    // exactly on the ends. NaN stays NaN through this and is stored as 0.
    out[i] = Math.round(v < 0 ? v * 32768 : v * 32767)
  }
  return out
}

/** Int16 samples to floats in [-1, 1], with the same scales as floatToInt16. */
export function int16ToFloat(x: Int16Array): Float32Array {
  const out = new Float32Array(x.length)
  for (let i = 0; i < x.length; i++) {
    out[i] = x[i] < 0 ? x[i] / 32768 : x[i] / 32767
  }
  return out
}
