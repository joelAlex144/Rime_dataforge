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
    this.enqueued = 0               // frames handed in so far: each chunk knows where it starts
    this.starts = []                // absolute start frame of each queued chunk
    this.port.onmessage = (e) => {
      const m = e.data
      if (m.type === 'pcm') {
        this.queue.push(m.pcm)
        this.starts.push(this.enqueued)
        this.enqueued += m.pcm.length
      } else if (m.type === 'truncate') {
        // Drop what is queued at and beyond an absolute frame index (a prompt
        // the listener answered: the rest of it is never played). What was
        // played stays counted; the chunk holding the cut is shortened.
        const at = m.atFrame
        const keep = []
        const keepStarts = []
        for (let k = 0; k < this.queue.length; k++) {
          const start = this.starts[k]
          const buf = this.queue[k]
          if (start + buf.length <= at) {
            keep.push(buf)
            keepStarts.push(start)
          } else if (start < at) {
            keep.push(buf.subarray(0, at - start))
            keepStarts.push(start)
          }
        }
        this.queue = keep
        this.starts = keepStarts
        this.enqueued = Math.max(at, this.frames)
        if (this.queue.length === 0) this.offset = 0
      } else if (m.type === 'flush') {
        // Drop what has not been played. Audible stop is immediate; the frame
        // counter keeps its value so the ack still says where we got to.
        this.queue = []
        this.starts = []
        this.offset = 0
        this.enqueued = this.frames
      } else if (m.type === 'reset') {
        this.queue = []
        this.starts = []
        this.offset = 0
        this.frames = 0
        this.enqueued = 0
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
        this.starts.shift()
        this.offset = 0
      }
    }
    this.port.postMessage({ type: 'frames', frames: this.frames })
    return true
  }
}

registerProcessor('pcm-player', PcmPlayer)
