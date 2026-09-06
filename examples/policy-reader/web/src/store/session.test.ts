import { describe, expect, it } from 'vitest'
import { AudioPlayer, b64ToInt16WithCarry } from './session'

/**
 * Rime /ws3 splits 1024-byte PCM blocks at arbitrary byte offsets. These are
 * the exact sizes seen on the wire (traces/preflight_20260905T163015Z.jsonl):
 * 829+195, 655+369, 1006+18. Four of the seven are an odd byte count.
 */
const RIME_SPLITS = [829, 195, 655, 369, 1006, 18, 1024]

function sineBytes(nSamples: number, hz = 440, sr = 24000): Uint8Array {
  const out = new Uint8Array(nSamples * 2)
  const view = new DataView(out.buffer)
  for (let i = 0; i < nSamples; i++) {
    view.setInt16(i * 2, Math.round(12000 * Math.sin((2 * Math.PI * hz * i) / sr)), true)
  }
  return out
}

function toB64(bytes: Uint8Array): string {
  let bin = ''
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i])
  return btoa(bin)
}

function split(bytes: Uint8Array, sizes: number[]): Uint8Array[] {
  const out: Uint8Array[] = []
  let at = 0
  for (const n of sizes) {
    out.push(bytes.slice(at, at + n))
    at += n
  }
  if (at !== bytes.length) throw new Error(`split mismatch ${at} != ${bytes.length}`)
  return out
}

describe('b64ToInt16WithCarry', () => {
  it('never throws on an odd byte count and carries the trailing byte', () => {
    const { samples, carry } = b64ToInt16WithCarry(toB64(new Uint8Array([1, 2, 3])), null)
    expect(samples.length).toBe(1)
    expect(carry).not.toBeNull()
    expect(carry![0]).toBe(3)
  })

  it('prepends the carried byte to the next chunk', () => {
    const a = b64ToInt16WithCarry(toB64(new Uint8Array([0x34, 0x12, 0x78])), null) // one sample + carry 0x78
    const b = b64ToInt16WithCarry(toB64(new Uint8Array([0x56])), a.carry)          // carry+1 = one sample
    expect(a.samples[0]).toBe(0x1234)
    expect(b.samples.length).toBe(1)
    expect(b.samples[0]).toBe(0x5678)
    expect(b.carry).toBeNull()
  })

  it('the stateless helper still works for even input', () => {
    const { samples } = b64ToInt16WithCarry(toB64(new Uint8Array([0x34, 0x12])), null)
    expect(samples[0]).toBe(0x1234)
  })
})

describe('AudioPlayer.pushB64 with real Rime chunk sizes', () => {
  it('enqueues exactly total_bytes / 2 frames despite odd-sized chunks', () => {
    // 2048 samples = 4096 bytes, split at Rime's observed offsets.
    const original = sineBytes(2048)
    const player = new AudioPlayer()
    for (const chunk of split(original, RIME_SPLITS)) player.pushB64(toB64(chunk))
    expect(player.enqueuedFrameCount).toBe(original.length / 2)
  })

  it('preserves sample alignment across the split boundaries', () => {
    // Reassemble what push() would have handed to the worklet and compare to
    // the source sine byte-for-byte. A one-byte shift decodes as noise.
    const original = sineBytes(2048)
    const player = new AudioPlayer()
    const got: number[] = []
    const origPush = player.push.bind(player)
    player.push = (pcm: Int16Array) => {
      for (let i = 0; i < pcm.length; i++) got.push(pcm[i])
      origPush(pcm)
    }
    for (const chunk of split(original, RIME_SPLITS)) player.pushB64(toB64(chunk))
    const expect16 = new Int16Array(original.buffer, original.byteOffset, original.length / 2)
    expect(got.length).toBe(expect16.length)
    for (let i = 0; i < got.length; i++) {
      if (got[i] !== expect16[i]) throw new Error(`sample ${i}: ${got[i]} != ${expect16[i]}`)
    }
  })

  it('a lone trailing byte at the end of the stream is not counted as a frame', () => {
    const original = new Uint8Array(4097)
    const player = new AudioPlayer()
    for (const chunk of split(original, [829, 195, 655, 369, 1006, 18, 1025])) {
      player.pushB64(toB64(chunk))
    }
    expect(player.enqueuedFrameCount).toBe(2048)
  })

  it('flush clears the carry so a stale half-sample cannot poison the next unit', () => {
    const player = new AudioPlayer()
    player.pushB64(toB64(new Uint8Array([1, 2, 3])))   // one frame enqueued, carry=3
    player.flush()
    // flush discards unplayed frames and resyncs the counter to what was
    // played (0 here: no worklet in jsdom). The carry must go with them, so
    // the next chunk decodes on its own boundary as exactly one clean sample.
    player.pushB64(toB64(new Uint8Array([4, 5])))
    expect(player.enqueuedFrameCount).toBe(1)
  })
})

/** No worklet in jsdom: the playhead is driven by hand, as the port message would. */
function playhead(player: AudioPlayer, frames: number) {
  ;(player as any).framesPlayed = frames
}

describe('unit_ended is the completion signal, never an interruption report', () => {
  it('a unit cut short by flush never fires unit_ended', () => {
    // Before this guard the flushed unit fired unit_ended with fewer frames than
    // the server sent, and the server logged a frame_count_mismatch for an
    // ordinary interruption. Six of those in traces/session_web-d2211205.jsonl.
    const player = new AudioPlayer()
    const ended: string[] = []
    ;(player as any).onUnitEnded = (c: string) => ended.push(c)
    player.beginUnit('u1')
    player.push(new Int16Array(4800))        // 200 ms sent
    playhead(player, 1200)                   // 50 ms heard when the listener stops it
    player.flush()
    player.beginUnit('u2')
    player.push(new Int16Array(2400))
    playhead(player, 99999)                  // playhead sails past everything
    ;(player as any).reportEndedUnits()
    player.markSynthComplete('u1')
    player.markSynthComplete('u2')
    expect(ended).not.toContain('u1')
    expect(ended).toEqual(['u2'])
  })

  it('unitDrainedMs is the exact frame span once the playhead is past the unit', () => {
    const player = new AudioPlayer()
    player.beginUnit('u1')
    player.push(new Int16Array(2400))        // exactly 100 ms at 24 kHz
    player.beginUnit('u2')
    player.push(new Int16Array(240))
    playhead(player, 1200)
    expect(player.unitDrainedMs('u1')).toBe(50)
    playhead(player, 2400)
    expect(player.unitDrainedMs('u1')).toBe(100)
    playhead(player, 99999)                  // never overshoots into the next unit
    expect(player.unitDrainedMs('u1')).toBe(100)
    expect(player.unitDrainedMs('nope')).toBe(0)
  })
})
