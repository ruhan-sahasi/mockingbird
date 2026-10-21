// For tests only: a stand-in for the AudioWorkletGlobalScope, so a worklet module can be loaded
// in Node and its processor driven by calling process() directly, one render quantum at a time.
// The scope's three globals are stubbed: AudioWorkletProcessor (whose port records what the
// processor posts), registerProcessor (which keeps the class) and sampleRate.

import { vi } from 'vitest'

export type Posted = { message: unknown; transfer: Transferable[] | undefined }

export class FakePort {
  /** Everything the processor has posted to the page, in order. */
  readonly posted: Posted[] = []
  onmessage: ((event: { data: unknown }) => void) | null = null

  postMessage(message: unknown, transfer?: Transferable[]): void {
    this.posted.push({ message, transfer })
  }

  /** Hand the processor a message, as the page's node.port.postMessage would. */
  deliver(data: unknown): void {
    this.onmessage?.({ data })
  }
}

export type FakeProcessor = {
  readonly port: FakePort
  process(inputs: Float32Array[][], outputs: Float32Array[][]): boolean
}

/**
 * Load the worklet module that `load` imports, and give a function that makes its processor
 * `name` at a sample rate. The rate stays set until the next processor is made, since a
 * processor may read `sampleRate` as it runs, so make one processor per test.
 */
export async function loadWorklet(
  name: string,
  load: () => Promise<unknown>,
): Promise<(rate: number) => FakeProcessor> {
  const registered = new Map<string, new () => FakeProcessor>()
  vi.stubGlobal(
    'AudioWorkletProcessor',
    class {
      readonly port = new FakePort()
    },
  )
  vi.stubGlobal('registerProcessor', (n: string, processor: new () => FakeProcessor) => {
    registered.set(n, processor)
  })
  await load()
  const Processor = registered.get(name)
  if (Processor === undefined) throw new Error(`the module registered no processor "${name}"`)
  return (rate) => {
    vi.stubGlobal('sampleRate', rate)
    return new Processor()
  }
}
