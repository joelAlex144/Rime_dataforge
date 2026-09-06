/**
 * One session store, shared by both routes.
 *
 * The listener page and the diagnostics page are two renderings of this state.
 * That is deliberate: if /dev showed a number the listener page did not have,
 * the two could disagree about what was heard, and the whole claim rests on
 * there being exactly one answer to that question.
 *
 * The rule the reducer enforces: `heard` advances only on acks from the audio
 * clock (`rendered`, `boundary`, `unit_done`), never on `audio` arriving. A
 * chunk landing in the browser is evidence that we sent it, nothing more.
 */

export type Cell = { state: 'ok' | 'warn' | 'down' | 'off'; detail: string }

export type StatusCells = {
  session_id?: string
  dev?: boolean
  replaying?: string | null
  provider?: Record<string, unknown>
  ingest?: Cell
  normalize?: Cell
  rime_ws?: Cell
  client_ws?: Cell
  stt?: Cell
  llm?: Cell
}

export type DocEntry = {
  name: string
  title: string
  section_count: number
  estimated_minutes: number
  progress: {
    started: boolean
    finished: boolean
    current_section_index: number
    minutes_left: number
  }
  referral: string
  unreviewed?: boolean
}

export type Unit = {
  unitId: string
  contextId: string
  index: number
  sectionTitle: string
  path?: string | null
  textDisplay: string
  sentences: number[][]
}

export type Answer = {
  question: string
  kind: string
  unitId: string | null
  answer: string
  referral: string
  offer: boolean
}

export type EventRecord = Record<string, unknown> & { type?: string; ts_ms?: number }

export type ContextRow = {
  context_id: string
  turn_id: number
  unit_id: string
  state: string
  bytes: number
  rendered_ms: number
  fenced_bytes: number
  audio_ms?: number
  ttfb_ms?: number | null
}

export type Metrics = {
  ttfb_p50: number | null
  ttfb_p95: number | null
  fenced_bytes_after_clear: number | null
  interpolated_spans: number | null
  spans_total: number | null
  flush_ack_p50: number | null
}

export type Phase = 'idle' | 'playing' | 'paused' | 'answering' | 'resuming' | 'finished'

export type State = {
  connected: boolean
  sessionId: string | null
  dev: boolean
  provider: Record<string, any>
  providerFellBack: boolean
  replaying: string | null

  documents: DocEntry[]
  current: string | null
  unreviewedOpen: string | null

  unit: Unit | null
  words: { words: string[]; start: number[]; end: number[] } | null
  // Char offsets into text_display with their audio times, so the read-along
  // highlight advances from the audio clock rather than waiting for a boundary
  // event (which only fires on an interrupt).
  spans: { char_start: number; char_end: number; t_start_ms: number; t_end_ms: number }[]
  renderedMs: number
  boundaryChar: number
  wordIndex: number
  heardUnits: Record<string, string>

  phase: Phase
  answer: Answer | null
  resume: { unitId: string; sentenceIndex: number; charStart: number; cue: string } | null
  error: string | null

  status: StatusCells
  contexts: ContextRow[]
  metrics: Metrics | null
  events: EventRecord[]
  ingestStages: { stage: string; state: string; detail: string }[]
}

export const EVENT_CAP = 500

/** The only message a successful reconnect is allowed to clear. */
export const DISCONNECTED = 'Lost the connection. Reconnecting.'
export const FELL_BACK = 'Couldn\u2019t reach the voice service. Playing with the fallback voice.'

export const initialState: State = {
  connected: false,
  sessionId: null,
  dev: false,
  provider: {},
  providerFellBack: false,
  replaying: null,
  documents: [],
  current: null,
  unreviewedOpen: null,
  unit: null,
  words: null,
  spans: [],
  renderedMs: 0,
  boundaryChar: 0,
  wordIndex: -1,
  heardUnits: {},
  phase: 'idle',
  answer: null,
  resume: null,
  error: null,
  status: {},
  contexts: [],
  metrics: null,
  events: [],
  ingestStages: [],
}

export type Action =
  | { type: 'connected'; value: boolean }
  | { type: 'server'; msg: any }
  | { type: 'status'; value: StatusCells }
  | { type: 'contexts'; value: ContextRow[] }
  | { type: 'metrics'; value: Metrics }
  | { type: 'documents'; value: DocEntry[] }
  | { type: 'askPending'; question: string }
  | { type: 'clearAnswer' }
  | { type: 'clearIngest' }
  | { type: 'error'; message: string | null }

/** How far through the clause the voice has actually got, in display chars.
 *
 * The end of the last word whose audio finished. A half-spoken word is not
 * counted, so the highlight never runs ahead of what was said.
 */
export function spokenCharsAt(
  spans: State['spans'], renderedMs: number,
): number {
  let at = 0
  for (const sp of spans) {
    if (sp.t_end_ms <= renderedMs) at = sp.char_end
    else break
  }
  return at
}

/** Which word is currently sounding, from the audio clock alone. */
export function wordAt(words: State['words'], renderedMs: number): number {
  if (!words) return -1
  for (let i = words.start.length - 1; i >= 0; i--) {
    if (renderedMs >= words.start[i]) return i
  }
  return -1
}

/**
 * The outgoing interrupt sequence. Order is not negotiable: the flush ack
 * carries the position the audio clock actually reached, so it has to be on
 * the wire before the interrupt that stops the clock. Sending them the other
 * way round measures the interrupt against a boundary that has already moved.
 */
export function buildInterrupt(contextId: string | null, renderedMs: number): any[] {
  return [
    { type: 'flush_ack', context_id: contextId, rendered_ms: renderedMs },
    { type: 'interrupt' },
  ]
}

/** Pause is an interrupt without a question: the same flush_ack first, so the
 *  server attributes the boundary to the clause being heard and re-reads it
 *  from the cursor instead of skipping it. */
export function buildPause(contextId: string | null, renderedMs: number): any[] {
  return [
    { type: 'flush_ack', context_id: contextId, rendered_ms: renderedMs },
    { type: 'pause' },
  ]
}

function push(events: EventRecord[], rec: EventRecord): EventRecord[] {
  const next = events.length >= EVENT_CAP ? events.slice(events.length - EVENT_CAP + 1) : events.slice()
  next.push(rec)
  return next
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'connected':
      // A reconnect clears the disconnect notice and nothing else: a provider
      // fallback is still true after the socket comes back.
      return {
        ...state,
        connected: action.value,
        error: action.value && state.error === DISCONNECTED ? null : state.error,
      }
    case 'status':
      return {
        ...state,
        status: action.value,
        dev: !!action.value.dev,
        replaying: action.value.replaying ?? null,
        // /dev can identify the session from the status poll alone, before any
        // websocket hello has arrived.
        sessionId: state.sessionId ?? action.value.session_id ?? null,
      }
    case 'contexts':
      return { ...state, contexts: action.value }
    case 'metrics':
      return { ...state, metrics: action.value }
    case 'documents':
      return { ...state, documents: action.value }
    case 'askPending':
      return { ...state, phase: 'answering', answer: null }
    case 'clearAnswer':
      return { ...state, answer: null }
    case 'clearIngest':
      return { ...state, ingestStages: [] }
    case 'error':
      return { ...state, error: action.message }
    case 'server':
      return applyServer(state, action.msg)
    default:
      return state
  }
}

function applyServer(state: State, m: any): State {
  switch (m?.type) {
    case 'hello':
      return {
        ...state,
        connected: true,
        sessionId: m.session_id,
        dev: !!m.dev,
        provider: m.provider || {},
        providerFellBack: (m.provider?.provider ?? '') === 'fake',
        documents: m.documents || [],
        current: m.current ?? null,
      }

    case 'provider_active': {
      const fell = (m.provider ?? '') === 'fake'
      return { ...state, provider: { ...m, type: undefined }, providerFellBack: fell }
    }

    case 'document_opened':
      return {
        ...state,
        current: m.name,
        documents: m.documents || state.documents,
        unit: null,
        words: null,
        renderedMs: 0,
        boundaryChar: 0,
        wordIndex: -1,
        answer: null,
        phase: 'idle',
      }

    case 'unit_started':
      return {
        ...state,
        unit: {
          unitId: m.unit_id,
          contextId: m.context_id,
          index: m.index,
          sectionTitle: m.section_title,
          path: m.path ?? null,
          textDisplay: m.text_display,
          sentences: m.sentences || [],
        },
        words: null,
        spans: [],
        renderedMs: 0,
        // A new unit starts entirely unspoken. Nothing is marked said until the
        // audio clock says so.
        boundaryChar: 0,
        wordIndex: -1,
        phase: 'playing',
      }

    case 'timestamps':
      return {
        ...state,
        words: { words: m.words, start: m.start_ms, end: m.end_ms },
        spans: m.spans || state.spans,
      }

    case 'rendered': {
      // Local echo of our own ack. Ignore one stamped with a different clause:
      // the queue is continuous, so an ack can be produced for a unit whose
      // audio is buffered but not yet on screen, and applying it would advance
      // the highlight of the clause the listener is still hearing.
      if (m.context_id && state.unit && m.context_id !== state.unit.contextId) {
        return state
      }
      // While playing, the read-along boundary follows the audio clock; an
      // interrupt's `boundary` event still overrides it.
      const renderedMs = m.rendered_ms ?? state.renderedMs
      const spoken = spokenCharsAt(state.spans, renderedMs)
      return {
        ...state,
        renderedMs,
        wordIndex: wordAt(state.words, renderedMs),
        boundaryChar: state.phase === 'playing' ? spoken : state.boundaryChar,
      }
    }

    case 'boundary': {
      const heard = { ...state.heardUnits }
      heard[m.unit_id] = m.char_end >= (m.of ?? 0) ? 'heard' : `truncated@${m.char_end}`
      return {
        ...state,
        boundaryChar: m.char_end,
        renderedMs: m.rendered_ms ?? state.renderedMs,
        wordIndex: m.word_index ?? state.wordIndex,
        heardUnits: heard,
        phase: 'paused',
      }
    }

    case 'unit_done':
      // Sent, not heard. The server still waits for acks before it calls this
      // unit heard, and so do we: only `unit_heard` moves the boundary.
      return state

    case 'unit_heard': {
      const heard = { ...state.heardUnits, [m.unit_id]: 'heard' }
      return { ...state, heardUnits: heard, boundaryChar: m.char_end ?? state.boundaryChar }
    }

    case 'playing':
      return { ...state, phase: 'playing', error: null }

    case 'paused':
      return { ...state, phase: state.phase === 'answering' ? 'answering' : 'paused' }

    case 'answer':
      return {
        ...state,
        phase: 'paused',
        answer: {
          question: m.question,
          kind: m.kind,
          unitId: m.unit_id ?? null,
          answer: m.answer,
          referral: m.referral,
          offer: !!m.offer,
        },
      }

    case 'resume_point':
      return {
        ...state,
        phase: 'resuming',
        resume: {
          unitId: m.unit_id,
          sentenceIndex: m.sentence_index,
          charStart: m.char_start,
          cue: m.cue,
        },
      }

    case 'document_finished':
      return { ...state, phase: 'finished' }

    case 'jumped':
      return { ...state, answer: null }

    case 'replay_start':
      return { ...state, replaying: m.trace, events: [] }

    case 'replay_end':
      return { ...state, replaying: null }

    case 'ingest_progress':
      return {
        ...state,
        ingestStages: [...state.ingestStages, { stage: m.stage, state: m.state, detail: m.detail }],
      }

    case 'provider_error':
      return { ...state, error: 'Couldn’t reach the voice service. Playing with the fallback voice.' }

    case 'error':
      return { ...state, error: m.message ?? null }

    case 'event': {
      const rec = m.record as EventRecord
      let next: State = { ...state, events: push(state.events, rec) }
      // Replayed traces drive the same views as a live session.
      if (rec?.type === 'unit_truncated') {
        next = {
          ...next,
          heardUnits: {
            ...next.heardUnits,
            [String(rec.context_id ?? rec.unit_id ?? '')]: `truncated@${rec.char_end}`,
          },
        }
      }
      if (rec?.type === 'provider_active') {
        next = { ...next, provider: rec as any, providerFellBack: (rec as any).provider === 'fake' }
      }
      return next
    }

    default:
      return state
  }
}
