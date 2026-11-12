// The speakers: the playback worklet behind an AudioContext. These stand in for the browser's
// audio objects, so what is checked is the player's own bookkeeping: a context that was opened
// is closed again whatever fails after it.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPlayer } from './player'

class FakeContext {
  static made: FakeContext[] = []
  static latency: { outputLatency?: number; baseLatency?: number } = {}
  state = 'suspended'
  outputLatency = FakeContext.latency.outputLatency
  baseLatency = FakeContext.latency.baseLatency
  closed = 0
  destination = {}
  audioWorklet = { addModule: async () => {} }
  constructor() {
    FakeContext.made.push(this)
  }
  async resume() {
    this.state = 'running'
  }
  async close() {
    this.closed++
  }
}

class FakeNode {
  static fail: Error | null = null
  static made: FakeNode[] = []
  posted: unknown[] = []
  port = {
    onmessage: null as unknown,
    close() {},
    postMessage: (message: unknown) => this.posted.push(message),
  }
  onprocessorerror: unknown = null
  constructor() {
    if (FakeNode.fail) throw FakeNode.fail
    FakeNode.made.push(this)
  }
  connect() {}
  disconnect() {}
}

beforeEach(() => {
  FakeContext.made = []
  FakeContext.latency = {}
  FakeNode.made = []
  FakeNode.fail = null
  vi.stubGlobal('AudioContext', FakeContext)
  vi.stubGlobal('AudioWorkletNode', FakeNode)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('createPlayer', () => {
  it('closes the context when the worklet node cannot be made', async () => {
    FakeNode.fail = new DOMException('no processor named playback', 'InvalidStateError')

    await expect(createPlayer()).rejects.toBe(FakeNode.fail)

    expect(FakeContext.made.map((c) => c.closed)).toEqual([1])
  })

  it('posts push and flush to the worklet, and closes the context on close()', async () => {
    const player = await createPlayer()
    const pcm = new Int16Array(320)

    player.push(pcm)
    player.flush()
    expect(FakeNode.made[0].posted).toEqual([{ type: 'push', pcm }, { type: 'flush' }])
    expect(FakeContext.made.map((c) => c.closed)).toEqual([0])

    player.close()
    expect(FakeContext.made.map((c) => c.closed)).toEqual([1])
  })

  it.each([
    ['the output latency', { outputLatency: 0.025, baseLatency: 0.01 }, 25],
    ['the base latency when there is no output latency', { baseLatency: 0.01 }, 10],
    ['the base latency when the output latency is 0', { outputLatency: 0, baseLatency: 0.01 }, 10],
    ['0 when the browser gives neither', {}, 0],
  ])('gives %s in ms', async (_case, latency, ms) => {
    FakeContext.latency = latency
    const player = await createPlayer()
    expect(player.outputLatencyMs()).toBeCloseTo(ms, 9)
  })
})
