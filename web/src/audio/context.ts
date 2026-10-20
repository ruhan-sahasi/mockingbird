// An AudioContext with one worklet module loaded and the audio clock running, shared by the mic
// and the player.

// How long to wait for the browser to start the audio clock. It starts at once once the page has
// had a click, which the Start button gives it.
const START_WAIT_MS = 2000

/** The browser would not start audio for this page. */
export class AudioBlockedError extends Error {
  constructor() {
    super('the browser did not start audio for this page')
    this.name = 'AudioBlockedError'
  }
}

/**
 * A context at the device's own sample rate, with the worklet at `moduleUrl` loaded. The device
 * rate matters for the mic: Firefox refuses to connect a mic to a context at any other rate. The
 * worklets resample to and from 16 kHz themselves. Throws AudioBlockedError if the audio clock
 * does not start, since a stopped clock records and plays nothing.
 */
export async function openWorkletContext(moduleUrl: string): Promise<AudioContext> {
  const ctx = new AudioContext()
  // Asked for straight away, while the click that led here is as recent as it can be. Whether it
  // worked is read from ctx.state below, so a rejection needs no handling of its own.
  const started = ctx.resume().catch(() => {})
  let timer: ReturnType<typeof setTimeout> | undefined
  try {
    await ctx.audioWorklet.addModule(moduleUrl)
    // A browser that will not start the clock leaves resume() pending, so it gets a time limit.
    await Promise.race([started, new Promise((resolve) => (timer = setTimeout(resolve, START_WAIT_MS)))])
    if (ctx.state !== 'running') throw new AudioBlockedError()
    return ctx
  } catch (error) {
    void ctx.close()
    throw error
  } finally {
    clearTimeout(timer)
  }
}
