/**
 * Client SDK. Runs on the main thread. Owns:
 *   - LiveKit room join
 *   - mic track up (for the listener's speech -> agent-side VAD/STT)
 *   - the data channel carrying protocol PCM/control messages
 *   - forwarding the AudioWorklet's acks upstream
 *
 * Deliberately thin: all delivery-truth logic (queueing, rendered-frame
 * counting, flush semantics) lives in playback-processor.js. This file
 * is plumbing between LiveKit and the worklet, plus (de)serializing
 * against playback_protocol.py's wire shape.
 *
 * Wire messages match playback_protocol.py:
 *   server -> client: unit_start, audio_chunk, word_timestamps, unit_done, cancel
 *   client -> server: playback_ack, flush_ack, client_error
 *
 * All timestamps in outgoing acks are ms, computed from the worklet's
 * renderedFrames using the unit's sample_rate_hz (from unit_start) --
 * never wall-clock time.
 */

import { Room, RoomEvent } from "livekit-client";

const PROTOCOL_VERSION = 1;

export class PolicyReaderClient {
  /**
   * @param {Object} opts
   * @param {string} opts.livekitUrl
   * @param {string} opts.token
   */
  constructor({ livekitUrl, token }) {
    this._livekitUrl = livekitUrl;
    this._token = token;
    this._room = new Room();
    this._audioContext = null;
    this._workletNode = null;
    // unit_id -> { sampleRateHz, turnId } , needed to convert the
    // worklet's renderedFrames (a frame count) into rendered_ms for
    // outgoing acks, since the worklet itself only knows frame counts.
    this._unitMeta = new Map();

    this._room.on(RoomEvent.DataReceived, (payload) => this._onData(payload));
  }

  async connect() {
    await this._room.connect(this._livekitUrl, this._token);
    await this._room.localParticipant.setMicrophoneEnabled(true);
    await this._setupAudioPipeline();
  }

  async disconnect() {
    await this._room.disconnect();
  }

  async _setupAudioPipeline() {
    this._audioContext = new AudioContext();
    await this._audioContext.audioWorklet.addModule(
      new URL("../worklet/playback-processor.js", import.meta.url)
    );
    this._workletNode = new AudioWorkletNode(this._audioContext, "playback-processor");
    this._workletNode.connect(this._audioContext.destination);
    this._workletNode.port.onmessage = (event) => this._onWorkletMessage(event.data);
  }

  // -- inbound: server -> client -------------------------------------------

  _onData(payload) {
    let msg;
    try {
      const text = new TextDecoder().decode(payload);
      msg = JSON.parse(text);
    } catch (err) {
      this._sendClientError(null, null, `failed to parse data message: ${err}`);
      return;
    }

    switch (msg.type) {
      case "unit_start":
        this._handleUnitStart(msg);
        break;
      case "audio_chunk":
        this._handleAudioChunk(msg);
        break;
      case "word_timestamps":
        // Word timestamps are consumed server-side for boundary
        // resolution (ledger.py); the client doesn't need them to
        // play audio, so nothing to do here beyond ignoring safely.
        break;
      case "unit_done":
        // Informational only -- delivery truth still comes from our
        // own acks, not from the server saying synthesis finished.
        break;
      case "cancel":
        this._handleCancel(msg);
        break;
      default:
        this._sendClientError(msg.turn_id ?? null, msg.unit_id ?? null, `unknown message type: ${msg.type}`);
    }
  }

  _handleUnitStart(msg) {
    this._unitMeta.set(msg.unit_id, {
      sampleRateHz: msg.sample_rate_hz,
      turnId: msg.turn_id,
    });
  }

  _handleAudioChunk(msg) {
    const meta = this._unitMeta.get(msg.unit_id);
    if (!meta) {
      this._sendClientError(msg.turn_id, msg.unit_id, "audio_chunk received before unit_start");
      return;
    }
    const pcmBytes = this._base64ToInt16(msg.pcm_b64);
    const samples = this._int16ToFloat32(pcmBytes);

    this._workletNode.port.postMessage(
      {
        cmd: "enqueue",
        turnId: msg.turn_id,
        unitId: msg.unit_id,
        seq: msg.seq,
        samples,
      },
      [samples.buffer]
    );
  }

  _handleCancel(msg) {
    this._workletNode.port.postMessage({
      cmd: "flush",
      turnId: msg.turn_id,
      unitId: msg.unit_id ?? null,
    });
  }

  // -- outbound: worklet -> server ------------------------------------------

  _onWorkletMessage(msg) {
    const meta = this._unitMeta.get(msg.unitId);
    const sampleRateHz = meta ? meta.sampleRateHz : this._audioContext.sampleRate;
    const renderedMs = Math.round((msg.renderedFrames / sampleRateHz) * 1000);

    if (msg.type === "playback_progress") {
      this._sendPlaybackAck(msg.turnId, msg.unitId, renderedMs, msg.audioClockTs);
    } else if (msg.type === "flush_ack") {
      this._sendFlushAck(msg.turnId, msg.unitId, renderedMs, msg.audioClockTs);
    }
  }

  _sendPlaybackAck(turnId, unitId, renderedMs, clockTs) {
    this._publish({
      type: "playback_ack",
      version: PROTOCOL_VERSION,
      turn_id: turnId,
      unit_id: unitId,
      rendered_ms: renderedMs,
      client_clock_ts: clockTs,
    });
  }

  _sendFlushAck(turnId, unitId, renderedMs, audibleStopTs) {
    this._publish({
      type: "flush_ack",
      version: PROTOCOL_VERSION,
      turn_id: turnId,
      unit_id: unitId,
      rendered_ms: renderedMs,
      audible_stop_ts: audibleStopTs,
    });
  }

  _sendClientError(turnId, unitId, message) {
    this._publish({
      type: "client_error",
      version: PROTOCOL_VERSION,
      turn_id: turnId,
      unit_id: unitId,
      message,
    });
  }

  _publish(obj) {
    const data = new TextEncoder().encode(JSON.stringify(obj));
    this._room.localParticipant.publishData(data, { reliable: true });
  }

  // -- codec helpers -----------------------------------------------------

  _base64ToInt16(b64) {
    const binary = atob(b64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Int16Array(bytes.buffer);
  }

  _int16ToFloat32(int16) {
    const out = new Float32Array(int16.length);
    for (let i = 0; i < int16.length; i++) out[i] = int16[i] / 32768;
    return out;
  }
}
