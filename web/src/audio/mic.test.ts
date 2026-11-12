// The mic: getUserMedia into the capture worklet. These stand in for the browser's audio objects,
// so what is checked is the mic's own bookkeeping: whatever fails after the mic is granted, the
// tracks are stopped and the context is closed, so the browser's recording light goes off.

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { startMic } from './mic'

class FakeTrack {
  stopped = 0
  stop() {
    this.stopped++
  }
}

class FakeContext {
  static made: FakeContext[] = []
  state = 'suspended'
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
  createMediaStreamSource() {
    return { connect() {}, disconnect() {} }
  }
}

class FakeNode {
  static fail: Error | null = null
  static made: FakeNode[] = []
  port = {
    onmessage: null as ((event: { data: unknown }) => void) | null,
    close() {},
  }
  onprocessorerror: unknown = null
  constructor() {
    if (FakeNode.fail) throw FakeNode.fail
    FakeNode.made.push(this)
  }
  connect() {}
  disconnect() {}
}

let tracks: FakeTrack[]

beforeEach(() => {
  tracks = [new FakeTrack(), new FakeTrack()]
  FakeContext.made = []
  FakeNode.made = []
  FakeNode.fail = null
  vi.stubGlobal('navigator', {
    mediaDevices: { getUserMedia: async () => ({ getTracks: () => tracks }) },
  })
  vi.stubGlobal('AudioContext', FakeContext)
  vi.stubGlobal('AudioWorkletNode', FakeNode)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe('startMic', () => {
  it('stops the tracks and closes the context when the worklet node cannot be made', async () => {
    FakeNode.fail = new DOMException('no processor named capture', 'InvalidStateError')

    await expect(startMic(() => {})).rejects.toBe(FakeNode.fail)

    expect(tracks.map((t) => t.stopped)).toEqual([1, 1])
    expect(FakeContext.made.map((c) => c.closed)).toEqual([1])
  })

  it('keeps the mic until stop(), and then lets go of it all', async () => {
    const mic = await startMic(() => {})
    expect(tracks.map((t) => t.stopped)).toEqual([0, 0])
    expect(FakeContext.made.map((c) => c.closed)).toEqual([0])

    mic.stop()

    expect(tracks.map((t) => t.stopped)).toEqual([1, 1])
    expect(FakeContext.made.map((c) => c.closed)).toEqual([1])
  })

  it('hands frames to onFrame and VAD changes to onVad, as the worklet tags them', async () => {
    const frames: Int16Array[] = []
    const vad: unknown[] = []
    await startMic((pcm) => void frames.push(pcm), (e) => void vad.push(e))
    const deliver = (data: unknown) => FakeNode.made[0].port.onmessage!({ data })
    const pcm = new Int16Array(320)

    deliver({ type: 'frame', pcm })
    deliver({ type: 'vad', speaking: true, loudMs: 60 })
    deliver({ type: 'vad', speaking: false, quietMs: 300 })

    expect(frames).toEqual([pcm])
    expect(frames[0]).toBe(pcm)
    expect(vad).toEqual([{ speaking: true, loudMs: 60 }, { speaking: false, quietMs: 300 }])
  })

  it('needs no onVad: frames still flow without one', async () => {
    const frames: Int16Array[] = []
    await startMic((pcm) => void frames.push(pcm))
    const deliver = (data: unknown) => FakeNode.made[0].port.onmessage!({ data })

    deliver({ type: 'vad', speaking: true })
    deliver({ type: 'frame', pcm: new Int16Array(320) })

    expect(frames).toHaveLength(1)
  })
})
