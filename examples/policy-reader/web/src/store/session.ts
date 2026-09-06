/**
 * WebSocket client and the audio player, shared by both routes.
 *
 * The player owns the only clock that matters. `rendered` acks are produced
 * from the AudioWorklet's own frame counter every 100 ms, not from a timer and
 * not from how much the server has sent. On interrupt, the flush ack carries
 * the worklet's frame count at that instant and goes out *before* the
 * interrupt -- see buildInterrupt() in reducer.ts.
 */
import { useCallback, useEffect, useMemo, useReducer, useRef } from 'react'
import { Action, DISCONNECTED, State, buildInterrupt, buildOpen, buildPause, initialState, reducer, wordAt } from './reducer'

const ACK_INTERVAL_MS = 100

export class AudioPlayer {
  ctx: AudioContext | null = null
  node: AudioWorkletNode | null = null
  sampleRate = 24000
  private framesPlayed = 0        // cumulative frames the worklet has emitted
  private enqueuedFrames = 0      // cumulative frames handed to the worklet
  // Frame index at which each unit's audio begins, in arrival order. The queue
  // is continuous, so the unit being HEARD is the last one whose audio has
  // started -- not the one whose audio most recently arrived. Acking the latter
  // puts every ack on the wrong clause: the server then never sees unit N
  // finish, times out, races ahead, and the text runs away from the voice.
  private units: { contextId: string; startFrame: number; ended: boolean }[] = []
  private onUnitEnded: ((contextId: string) => void) | null = null
  private ackTimer: number | null = null
  private onAck: ((ms: number) => void) | null = null
  ready = false
  // Rime /ws3 splits its 1024-byte PCM blocks at arbitrary byte offsets, so a
  // chunk can end halfway through an s16le sample. An odd trailing byte is held
  // here and prepended to the next chunk. Without this, Int16Array on an odd
  // buffer throws, and inside an async handler that rejection is silent: both
  // halves of the split block vanish, heard as ~20 ms holes in the speech.
  private carry: Uint8Array | null = null
  // A burst of audio messages must not create several AudioContexts. The
  // first caller starts; everyone else awaits the same in-flight promise.
  private starting: Promise<void> | null = null

  async start(
    sampleRate: number,
    onAck: (ms: number) => void,
    onUnitEnded?: (contextId: string) => void,
  ) {
    this.sampleRate = sampleRate
    this.onAck = onAck
    this.onUnitEnded = onUnitEnded || null
    if (this.ready) return
    if (this.starting) return this.starting
    if (typeof AudioContext === 'undefined') return // jsdom: tests drive the reducer directly
    this.starting = this.doStart(sampleRate)
    try {
      await this.starting
    } finally {
      this.starting = null
    }
  }

  private async doStart(sampleRate: number) {
    this.ctx = new AudioContext({ sampleRate })
    await this.ctx.audioWorklet.addModule('/player-worklet.js')
    this.node = new AudioWorkletNode(this.ctx, 'pcm-player', { outputChannelCount: [1] })
    this.node.port.onmessage = (e) => {
      if (e.data?.type === 'frames') this.framesPlayed = e.data.frames
    }
    this.node.connect(this.ctx.destination)
    // A context created outside a user gesture starts suspended, and the play
    // click happened before this context existed. Resume it here or every
    // chunk is rendered into a stopped graph and nothing is audible.
    if (this.ctx.state === 'suspended') await this.ctx.resume()
    this.ready = true
    this.ackTimer = window.setInterval(() => {
      this.reportEndedUnits()
      this.onAck?.(this.renderedMs())
    }, ACK_INTERVAL_MS)
  }

  /** Milliseconds of the CURRENT unit the worklet has actually emitted.
   *
   * Frames are counted cumulatively and never reset mid-stream, because the
   * queue is continuous: the next unit's audio is enqueued while this one is
   * still sounding. Per-unit position is the cumulative count minus the frame
   * this unit started at.
   */
  private playheadIndex(): number {
    for (let i = this.units.length - 1; i >= 0; i--) {
      if (this.units[i].startFrame <= this.framesPlayed) return i
    }
    return this.units.length ? 0 : -1
  }

  /** Frame at which unit i ends: where the next one starts, or all we have. */
  private unitEndFrame(i: number): number {
    return i + 1 < this.units.length ? this.units[i + 1].startFrame : this.enqueuedFrames
  }

  /** The unit currently sounding, from the audio clock. */
  playheadUnit(): { contextId: string; startFrame: number } | null {
    const i = this.playheadIndex()
    return i < 0 ? null : this.units[i]
  }

  /** Milliseconds of the CURRENT unit played, clamped to that unit's length.
   *
   * Without the clamp the count runs on into the next clause's audio while
   * still attributed to this one, the server sees the unit "finish" early and
   * starts the next, and the text runs ahead of the voice.
   */
  renderedMs(): number {
    const i = this.playheadIndex()
    if (i < 0) return 0
    const u = this.units[i]
    const span = Math.max(0, this.unitEndFrame(i) - u.startFrame)
    const frames = Math.min(Math.max(0, this.framesPlayed - u.startFrame), span)
    return (frames / this.sampleRate) * 1000
  }

  /** Known audio length of a unit, in ms, from the frames the client holds. */
  unitAudioMs(contextId: string): number {
    const i = this.units.findIndex((u) => u.contextId === contextId)
    if (i < 0) return 0
    return ((this.unitEndFrame(i) - this.units[i].startFrame) / this.sampleRate) * 1000
  }

  /** Fire unit_ended once for every unit whose last sample has been emitted.
   *
   * The playhead moves to the next unit before an ack ever reports the previous
   * one at 100%, so without this the server would see each clause plateau a few
   * percent short and never mark it heard. This is the definitive completion
   * signal: the client knows exactly when it has drained a unit's frames.
   */
  private reportEndedUnits() {
    for (let i = 0; i < this.units.length; i++) {
      const u = this.units[i]
      if (u.ended) continue
      const end = this.unitEndFrame(i)
      // Ended only once its audio has arrived (a later unit has started, so
      // this one's end frame is fixed) and the playhead has passed it.
      const fixed = i + 1 < this.units.length
      if (fixed && this.framesPlayed >= end) {
        u.ended = true
        this.onUnitEnded?.(u.contextId)
      }
    }
  }

  /** Exact ms of a unit the client has drained: its full frame span once the
   *  playhead is past it. The client measured this; it is not an estimate. */
  unitDrainedMs(contextId: string): number {
    const i = this.units.findIndex((u) => u.contextId === contextId)
    if (i < 0) return 0
    const span = this.unitEndFrame(i) - this.units[i].startFrame
    const played = Math.min(Math.max(0, this.framesPlayed - this.units[i].startFrame), span)
    return (played / this.sampleRate) * 1000
  }

  /** The last unit has no successor to fix its end frame; call on unit_done. */
  markSynthComplete(contextId: string) {
    const i = this.units.findIndex((u) => u.contextId === contextId)
    if (i < 0) return
    if (this.framesPlayed >= this.unitEndFrame(i) && !this.units[i].ended) {
      this.units[i].ended = true
      this.onUnitEnded?.(contextId)
    }
  }

  push(pcm: Int16Array) {
    if (pcm.length === 0) return
    this.enqueuedFrames += pcm.length
    this.node?.port.postMessage({ type: 'pcm', pcm }, [pcm.buffer])
  }

  /** Decode a base64 PCM chunk with the odd-byte carry, then enqueue it. */
  pushB64(b64: string) {
    const { samples, carry } = b64ToInt16WithCarry(b64, this.carry)
    this.carry = carry
    this.push(samples)
  }

  /** Frames handed to the worklet so far, all units. */
  get enqueuedFrameCount(): number {
    return this.enqueuedFrames
  }

  /** Frames enqueued for ONE unit. The server checks this against the bytes it
   *  sent for that context; a shortfall means a chunk was dropped in transit
   *  and the unit must not be marked heard. */
  unitFrames(contextId: string): number {
    const i = this.units.findIndex((u) => u.contextId === contextId)
    if (i < 0) return 0
    return Math.max(0, this.unitEndFrame(i) - this.units[i].startFrame)
  }

  /** A new unit's audio starts after everything already queued.
   *
   * This must NOT clear the queue. Doing so discards the tail of the clause
   * that is still playing, which is heard as a sentence cutting off partway
   * and jumping to the next one.
   */
  beginUnit(contextId: string) {
    this.units.push({ contextId, startFrame: this.enqueuedFrames, ended: false })
  }

  /** Drop everything queued but not yet played, and report where we stopped.
   *
   * Only an interrupt does this. Dropped frames are never played, so the
   * enqueued counter is resynced to what was actually heard.
   */
  flush(): number {
    const at = this.renderedMs()
    this.node?.port.postMessage({ type: 'flush' })
    // Dropped frames are never played: resync, and forget units whose audio
    // was discarded so the playhead cannot point past what was heard.
    this.enqueuedFrames = this.framesPlayed
    this.units = this.units.filter((u) => u.startFrame <= this.framesPlayed)
    // A unit still in the list after a flush was cut short. Its outcome is
    // reported by the flush_ack / unit_truncated path; it must never fire
    // unit_ended, or the server would see a frame shortfall and log it as a
    // mismatch. That is a correct refusal on the wrong channel: it makes real
    // chunk loss indistinguishable from an ordinary interruption.
    for (const u of this.units) u.ended = true
    this.carry = null
    return at
  }

  async pause() {
    await this.ctx?.suspend()
  }

  async resume() {
    // Safe before the context exists: start() resumes it on creation.
    if (this.ctx && this.ctx.state !== 'running') await this.ctx.resume()
  }

  /** For the UI: 'running' once audio can actually be heard. */
  get contextState(): string {
    return this.ctx?.state ?? 'none'
  }

  stop() {
    this.carry = null
    if (this.ackTimer !== null) window.clearInterval(this.ackTimer)
    this.ackTimer = null
    this.node?.disconnect()
    this.ctx?.close()
    this.ready = false
  }
}

/**
 * Decode base64 s16le PCM, carrying an odd trailing byte to the next call.
 *
 * `new Int16Array(buf)` throws RangeError when the byte length is odd. Rime
 * splits blocks at arbitrary offsets, so that happens on roughly 8% of chunks.
 * The returned `carry` is the unpaired byte, if any; pass it back in with the
 * next chunk.
 */
export function b64ToInt16WithCarry(
  b64: string,
  carry: Uint8Array | null,
): { samples: Int16Array; carry: Uint8Array | null } {
  const bin = atob(b64)
  const lead = carry ? carry.length : 0
  const total = lead + bin.length
  const even = total - (total % 2)
  const buf = new ArrayBuffer(even)
  const bytes = new Uint8Array(buf)
  if (carry) bytes.set(carry, 0)
  const take = even - lead
  for (let i = 0; i < take; i++) bytes[lead + i] = bin.charCodeAt(i)
  const nextCarry =
    total % 2 === 1 ? new Uint8Array([bin.charCodeAt(bin.length - 1)]) : null
  return { samples: new Int16Array(buf), carry: nextCarry }
}

/** Stateless decode for callers that hold no carry. Kept for compatibility. */
export function b64ToInt16(b64: string): Int16Array {
  return b64ToInt16WithCarry(b64, null).samples
}

export type Session = {
  state: State
  dispatch: React.Dispatch<Action>
  send: (msg: any) => void
  interrupt: () => void
  play: () => void
  pause: () => void
  ask: (q: string) => void
  resume: () => void
  open: (name: string) => void
  jump: (unitId: string) => void
  player: AudioPlayer
}

export function useSession(): Session {
  const [state, dispatch] = useReducer(reducer, initialState)
  const wsRef = useRef<WebSocket | null>(null)
  const playerRef = useRef<AudioPlayer>(new AudioPlayer())
  const ctxRef = useRef<string | null>(null)
  // Whether THIS tab is the session's audio sink. The server sends audio to
  // exactly one socket; every other tab shows the same state silently.
  const sinkRef = useRef(false)
  // Units and their word maps arrive while the PREVIOUS clause is still
  // sounding. Showing them on arrival is what makes the text run ahead of the
  // voice, so they are held here and released when the playhead reaches them.
  const pendingUnits = useRef<Map<string, any>>(new Map())
  const pendingTs = useRef<Map<string, any>>(new Map())
  const shownCtx = useRef<string | null>(null)

  const send = useCallback((msg: any) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg))
  }, [])

  // Scale a timestamps message onto the audio length the client actually holds
  // for that unit, so the read-along highlight tracks the voice from the first
  // ack instead of following Rime's prediction (which undershoots) until the
  // server's corrected map arrives.
  const scaleTs = useCallback((ts: any) => {
    if (!ts || !ts.spans || !ts.spans.length) return ts
    const known = playerRef.current.unitAudioMs(ts.context_id)
    const last = ts.spans[ts.spans.length - 1].t_end_ms
    if (!known || !last) return ts
    const f = known / last
    if (Math.abs(f - 1) < 0.02) return ts
    return {
      ...ts,
      start_ms: (ts.start_ms || []).map((v: number) => v * f),
      end_ms: (ts.end_ms || []).map((v: number) => v * f),
      spans: ts.spans.map((sp: any) => ({
        ...sp, t_start_ms: sp.t_start_ms * f, t_end_ms: sp.t_end_ms * f,
      })),
    }
  }, [])

  useEffect(() => {
    const proto = location.protocol === 'https:' ? 'wss:' : 'ws:'
    const ws = new WebSocket(`${proto}//${location.host}/ws/audio`)
    wsRef.current = ws

    ws.onopen = () => dispatch({ type: 'connected', value: true })
    ws.onclose = () => {
      dispatch({ type: 'connected', value: false })
      dispatch({ type: 'error', message: DISCONNECTED })
    }
    ws.onmessage = async (ev) => {
      let m: any = null
      try {
        m = JSON.parse(ev.data)
        await handleMessage(m)
      } catch (e) {
        // An async handler's rejection goes nowhere. A dropped message must be
        // loud: it is exactly how a whole block of audio disappeared unnoticed.
        const kind = m?.type ?? 'unparseable'
        console.error(`ws message handler failed (type=${kind})`, e)
        dispatch({ type: 'error', message: 'A playback message was lost. Audio may have gaps.' })
      }
    }
    // The unit the listener is hearing right now. Not ctxRef: under lookahead
    // the most recently ARRIVED audio is the next clause, and a flush ack
    // stamped with it attributed the boundary to a clause not yet sounding.
    const playheadCtx = () => playerRef.current.playheadUnit()?.contextId ?? ctxRef.current

    const handleMessage = async (m: any) => {
      if (m.type === 'sink') {
        sinkRef.current = !!m.you
        dispatch({ type: 'server', msg: m })
        return
      }
      if (m.type === 'flush') {
        // A stop from another tab. Flush here, where the audio is, and report
        // the playhead so the server can attribute the boundary.
        const ctx = playheadCtx()
        const at = playerRef.current.flush()
        send({ type: 'flush_ack', context_id: ctx, rendered_ms: at })
        return
      }
      if (m.type === 'audio') {
        if (!sinkRef.current) return          // not this tab's voice
        ctxRef.current = m.context_id
        try {
          await playerRef.current.start(
            m.sample_rate || 24000,
            (ms) => {
              const at = playerRef.current.playheadUnit()
              const ctx = at ? at.contextId : ctxRef.current
              // The displayed clause changes when the PLAYHEAD moves into it.
              if (ctx && ctx !== shownCtx.current) {
                const started = pendingUnits.current.get(ctx)
                if (started) {
                  dispatch({ type: 'server', msg: started })
                  shownCtx.current = ctx
                  const ts = pendingTs.current.get(ctx)
                  if (ts) dispatch({ type: 'server', msg: scaleTs(ts) })
                }
              }
              dispatch({
                type: 'server',
                msg: { type: 'rendered', rendered_ms: ms, context_id: ctx },
              })
              send({
                type: 'rendered',
                context_id: ctx,
                rendered_ms: ms,
                enqueued_frames: ctx ? playerRef.current.unitFrames(ctx) : 0,
              })
            },
            (endedCtx) => {
              // Definitive completion for a unit: the client emitted its last
              // sample. The server marks it heard on this rather than on an ack
              // that plateaus short when the playhead has already moved on.
              send({
                type: 'unit_ended',
                context_id: endedCtx,
                enqueued_frames: playerRef.current.unitFrames(endedCtx),
                // The client's own drained count. The periodic ack lands up to
                // 100 ms short of the end; this is the exact final value.
                rendered_ms: playerRef.current.unitDrainedMs(endedCtx),
              })
            },
          )
        } catch (e) {
          // A failure here is total silence with no other symptom: no worklet,
          // no playback, no acks, and the server waiting on rendered_ms that
          // never arrives. Say so rather than letting the promise reject into
          // nothing.
          dispatch({
            type: 'error',
            message: 'Audio could not start in this browser. Playback is unavailable.',
          })
          console.error('AudioWorklet failed to start', e)
          return
        }
        playerRef.current.pushB64(m.b64)
        return
      }
      if (m.type === 'unit_started') {
        if (!sinkRef.current) {
          // No playhead here to release it: show it as it happens.
          dispatch({ type: 'server', msg: m })
          return
        }
        playerRef.current.beginUnit(m.context_id)
        pendingUnits.current.set(m.context_id, m)
        // Held, not shown: the playhead releases it (see the ack callback).
        // Nothing is queued yet on the very first unit, so show it immediately.
        if (shownCtx.current === null) {
          shownCtx.current = m.context_id
          dispatch({ type: 'server', msg: m })
        }
        return
      }
      if (m.type === 'timestamps') {
        if (!sinkRef.current) {
          dispatch({ type: 'server', msg: m })
          return
        }
        pendingTs.current.set(m.context_id, m)
        if (m.context_id === shownCtx.current) dispatch({ type: 'server', msg: scaleTs(m) })
        return
      }
      if (m.type === 'unit_done') {
        // The last unit has no successor to fix its end frame; tell the player
        // synthesis is complete so it can still fire unit_ended for it.
        playerRef.current.markSynthComplete(m.context_id)
        dispatch({ type: 'server', msg: m })
        return
      }
      dispatch({ type: 'server', msg: m })
    }
    return () => {
      ws.close()
      playerRef.current.stop()
    }
  }, [send])

  // Poll the three diagnostics endpoints. /dev also updates from events.
  useEffect(() => {
    let alive = true
    const tick = async () => {
      try {
        const [s, c, mt] = await Promise.all([
          fetch('/api/status').then((r) => r.json()),
          fetch('/api/contexts').then((r) => r.json()),
          fetch('/api/metrics').then((r) => r.json()),
        ])
        if (!alive) return
        dispatch({ type: 'status', value: s })
        dispatch({ type: 'contexts', value: c.contexts || [] })
        dispatch({ type: 'metrics', value: mt })
      } catch {
        /* the status strip simply stays stale */
      }
    }
    tick()
    const id = window.setInterval(tick, 2000)
    return () => {
      alive = false
      window.clearInterval(id)
    }
  }, [])

  const interrupt = useCallback(() => {
    // Flush locally first so the ack carries a position that has stopped
    // moving, stamped with the unit at the playhead (not the last to arrive).
    const ctx = playerRef.current.playheadUnit()?.contextId ?? ctxRef.current
    const at = playerRef.current.flush()
    for (const msg of buildInterrupt(ctx, at)) send(msg)
  }, [send])

  const play = useCallback(() => {
    playerRef.current.resume()
    send({ type: 'play' })
  }, [send])

  const pause = useCallback(() => {
    const ctx = playerRef.current.playheadUnit()?.contextId ?? ctxRef.current
    const at = playerRef.current.flush()
    for (const msg of buildPause(ctx, at)) send(msg)
  }, [send])

  const ask = useCallback(
    (q: string) => {
      dispatch({ type: 'askPending', question: q })
      send({ type: 'ask', question: q })
    },
    [send],
  )

  const resume = useCallback(() => send({ type: 'resume' }), [send])
  const open = useCallback(
    (name: string) => {
      const sounding = playerRef.current.playheadUnit() !== null
      const ctx = playerRef.current.playheadUnit()?.contextId ?? ctxRef.current
      const at = sounding ? playerRef.current.flush() : 0
      for (const msg of buildOpen(name, ctx, at, sounding)) send(msg)
    },
    [send],
  )
  const jump = useCallback((unitId: string) => send({ type: 'jump', unit_id: unitId }), [send])

  return useMemo(
    () => ({
      state,
      dispatch,
      send,
      interrupt,
      play,
      pause,
      ask,
      resume,
      open,
      jump,
      player: playerRef.current,
    }),
    [state, send, interrupt, play, pause, ask, resume, open, jump],
  )
}

export { wordAt }
