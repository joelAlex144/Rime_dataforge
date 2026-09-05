/**
 * PCM player worklet.
 *
 * It counts the frames it has actually written to the output and reports that
 * number back. That counter is the delivery clock for the whole system: the
 * `rendered` acks, the flush ack that fixes the interrupt boundary, and the
 * "heard" state in the ledger all derive from it. Nothing here estimates.
 */
class PcmPlayer extends AudioWorkletProcessor {
  constructor() {
    super()
    this.queue = []
    this.offset = 0
    this.frames = 0
    this.port.onmessage = (e) => {
      const m = e.data
      if (m.type === 'pcm') {
        this.queue.push(m.pcm)
      } else if (m.type === 'flush') {
        // Drop what has not been played. Audible stop is immediate; the frame
        // counter keeps its value so the ack still says where we got to.
        this.queue = []
        this.offset = 0
      } else if (m.type === 'reset') {
        this.queue = []
        this.offset = 0
        this.frames = 0
      }
    }
  }

  process(_inputs, outputs) {
    const out = outputs[0][0]
    if (!out) return true
    let i = 0
    while (i < out.length) {
      if (this.queue.length === 0) {
        out[i++] = 0
        continue
      }
      const buf = this.queue[0]
      out[i++] = buf[this.offset++] / 32768
      this.frames++
      if (this.offset >= buf.length) {
        this.queue.shift()
        this.offset = 0
      }
    }
    this.port.postMessage({ type: 'frames', frames: this.frames })
    return true
  }
}

registerProcessor('pcm-player', PcmPlayer)
