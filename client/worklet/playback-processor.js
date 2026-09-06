/**
 * AudioWorkletProcessor: the only place in the system that can honestly
 * say "this was heard." Runs on the audio rendering thread, not the
 * main thread -- so this file has no access to the DOM, LiveKit, or the
 * network. It only ever talks to the SDK (client/sdk/) via port messages.
 *
 * Responsibilities (per spec, nothing more):
 *   - PCM queue per unit
 *   - a samples-rendered counter (not samples-enqueued -- see below)
 *   - an ack every 100ms during normal playback
 *   - on flush: drop the queue immediately and emit an immediate ack
 *     whose audible_stop_ts comes from the audio context clock
 *     (currentTime), not wall-clock time
 *
 * Everything downstream (fence.py's result_fenced, ledger.py's boundary
 * resolution) trusts renderedFrames as ground truth. If this counter
 * ever counted samples pushed into the queue instead of samples that
 * actually passed through process()'s output, the whole delivery-aware
 * claim would be lying about what happened.
 */

const ACK_INTERVAL_SEC = 0.1; // 100ms, per spec

class PlaybackProcessor extends AudioWorkletProcessor {
  constructor() {
    super();

    // One queue entry per (turnId, unitId): { turnId, unitId, chunks: Float32Array[], renderedFrames, chunkSampleRate }
    // Chunks for a unit are consumed strictly in order; a unit is only
    // considered "current" once it reaches the head of playOrder.
    this._playOrder = []; // array of unit keys, in dispatch order
    this._units = new Map(); // key -> unit state
    this._lastAckTime = 0;

    this.port.onmessage = (event) => this._handleMessage(event.data);
  }

  _unitKey(turnId, unitId) {
    return `${turnId}:${unitId}`;
  }

  _handleMessage(msg) {
    if (msg.cmd === "enqueue") {
      this._enqueue(msg);
    } else if (msg.cmd === "flush") {
      this._flush(msg);
    }
  }

  _enqueue({ turnId, unitId, seq, samples }) {
    const key = this._unitKey(turnId, unitId);
    let unit = this._units.get(key);
    if (!unit) {
      unit = { turnId, unitId, chunks: [], renderedFrames: 0, nextSeq: 0 };
      this._units.set(key, unit);
      this._playOrder.push(key);
    }
    // Chunks may arrive slightly out of order over the data channel;
    // buffer by seq and only append contiguous ones. Non-contiguous
    // arrivals are held until their predecessor shows up.
    unit.chunks.push({ seq, samples });
    unit.chunks.sort((a, b) => a.seq - b.seq);
  }

  _flush({ turnId, unitId }) {
    // unitId === null/undefined means "flush everything" (a full-turn
    // cancel); otherwise only that unit's queue is dropped.
    const keysToFlush =
      unitId == null
        ? [...this._units.keys()]
        : [this._unitKey(turnId, unitId)].filter((k) => this._units.has(k));

    const audibleStopTs = currentTime; // AudioWorkletGlobalScope global, audio-clock seconds

    for (const key of keysToFlush) {
      const unit = this._units.get(key);
      if (!unit) continue;
      // Drop the queue immediately -- no further samples from this
      // unit will ever be rendered.
      unit.chunks = [];
      this.port.postMessage({
        type: "flush_ack",
        turnId: unit.turnId,
        unitId: unit.unitId,
        renderedFrames: unit.renderedFrames,
        audioClockTs: audibleStopTs,
      });
      this._units.delete(key);
      const idx = this._playOrder.indexOf(key);
      if (idx !== -1) this._playOrder.splice(idx, 1);
    }
  }

  _currentUnit() {
    while (this._playOrder.length > 0) {
      const key = this._playOrder[0];
      const unit = this._units.get(key);
      if (!unit) {
        this._playOrder.shift();
        continue;
      }
      return unit;
    }
    return null;
  }

  process(inputs, outputs) {
    const output = outputs[0];
    const channel = output[0]; // mono; CHANNELS=1 per protocol
    const framesNeeded = channel.length;

    let framesWritten = 0;
    while (framesWritten < framesNeeded) {
      const unit = this._currentUnit();
      if (!unit) {
        // Nothing to play -- fill remaining with silence.
        channel.fill(0, framesWritten);
        break;
      }
      // Only consume contiguous chunks (seq === nextSeq); a gap means
      // we wait rather than skip ahead and misreport rendered_ms.
      if (unit.chunks.length === 0 || unit.chunks[0].seq !== unit.nextSeq) {
        channel.fill(0, framesWritten);
        break;
      }
      const chunk = unit.chunks[0];
      const available = chunk.samples.length - (chunk._offset || 0);
      const toCopy = Math.min(available, framesNeeded - framesWritten);
      const offset = chunk._offset || 0;

      channel.set(chunk.samples.subarray(offset, offset + toCopy), framesWritten);

      framesWritten += toCopy;
      unit.renderedFrames += toCopy; // samples-rendered counter: ONLY incremented here
      chunk._offset = offset + toCopy;

      if (chunk._offset >= chunk.samples.length) {
        unit.chunks.shift();
        unit.nextSeq += 1;
      }
    }

    // Ack every ~100ms of wall-audio-time, per spec.
    if (currentTime - this._lastAckTime >= ACK_INTERVAL_SEC) {
      this._lastAckTime = currentTime;
      for (const key of this._playOrder) {
        const unit = this._units.get(key);
        if (!unit) continue;
        this.port.postMessage({
          type: "playback_progress",
          turnId: unit.turnId,
          unitId: unit.unitId,
          renderedFrames: unit.renderedFrames,
          audioClockTs: currentTime,
        });
      }
    }

    return true; // keep processor alive
  }
}

registerProcessor("playback-processor", PlaybackProcessor);
