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
import { Action, DISCONNECTED, State, buildInterrupt, initialState, reducer, wordAt } from './reducer'

const ACK_INTERVAL_MS = 100

export class AudioPlayer {
  ctx: AudioContext | null = null
  node: AudioWorkletNode | null = null
  sampleRate = 24000
  private framesPlayed = 0
  private ackTimer: number | null = null
  private onAck: ((ms: number) => void) | null = null
  ready = false

  async start(sampleRate: number, onAck: (ms: number) => void) {
    this.sampleRate = sampleRate
    this.onAck = onAck
    if (this.ready) return
    if (typeof AudioContext === 'undefined') return // jsdom: tests drive the reducer directly
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
      this.onAck?.(this.renderedMs())
    }, ACK_INTERVAL_MS)
  }

  /** Milliseconds of audio the worklet has actually emitted. */
  renderedMs(): number {
    return (this.framesPlayed / this.sampleRate) * 1000
  }

  push(pcm: Int16Array) {
    this.node?.port.postMessage({ type: 'pcm', pcm }, [pcm.buffer])
  }

  /** Drop everything queued but not yet played, and report where we stopped. */
  flush(): number {
    const at = this.renderedMs()
    this.node?.port.postMessage({ type: 'flush' })
    return at
  }

  resetUnit() {
    this.framesPlayed = 0
    this.node?.port.postMessage({ type: 'reset' })
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
    if (this.ackTimer !== null) window.clearInterval(this.ackTimer)
    this.ackTimer = null
    this.node?.disconnect()
    this.ctx?.close()
    this.ready = false
  }
}

export function b64ToInt16(b64: string): Int16Array {
  const bin = atob(b64)
  const bytes = new Uint8Array(bin.length)
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i)
  return new Int16Array(bytes.buffer)
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

  const send = useCallback((msg: any) => {
    const ws = wsRef.current
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(msg))
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
      const m = JSON.parse(ev.data)
      if (m.type === 'audio') {
        ctxRef.current = m.context_id
        await playerRef.current.start(m.sample_rate || 24000, (ms) => {
          dispatch({ type: 'server', msg: { type: 'rendered', rendered_ms: ms } })
          send({ type: 'rendered', context_id: ctxRef.current, rendered_ms: ms })
        })
        playerRef.current.push(b64ToInt16(m.b64))
        return
      }
      if (m.type === 'unit_started') playerRef.current.resetUnit()
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
    // Flush locally first so the ack carries a position that has stopped moving.
    const at = playerRef.current.flush()
    for (const msg of buildInterrupt(ctxRef.current, at)) send(msg)
  }, [send])

  const play = useCallback(() => {
    playerRef.current.resume()
    send({ type: 'play' })
  }, [send])

  const pause = useCallback(() => {
    playerRef.current.flush()
    send({ type: 'pause' })
  }, [send])

  const ask = useCallback(
    (q: string) => {
      dispatch({ type: 'askPending', question: q })
      send({ type: 'ask', question: q })
    },
    [send],
  )

  const resume = useCallback(() => send({ type: 'resume' }), [send])
  const open = useCallback((name: string) => send({ type: 'open', name }), [send])
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
