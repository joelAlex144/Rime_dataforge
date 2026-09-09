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
  enrich_rest?: { enabled: boolean; navigator_waiting: number }
  upload_enabled?: boolean
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
  spoken_title?: string
  unreviewed?: boolean
  doc_id?: string
  reviewed?: boolean
  readable?: boolean
  navigator?: 'ready' | 'preparing' | 'mechanical'
}

export type Unit = {
  unitId: string
  contextId: string
  index: number
  sectionTitle: string
  path?: string | null
  textDisplay: string
  sentences: number[][]
  // A clause resumed after a cut is synthesised from here; everything before
  // it was already heard and stays ink.
  charStart: number
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

// 'answering': question sent, answer audio not yet started. 'speaking': the
// answer's own audio is sounding (its unit is client-acked like any clause).
export type Phase = 'idle' | 'playing' | 'paused' | 'answering' | 'speaking' | 'resuming' | 'finished'

export type State = {
  connected: boolean
  sessionId: string | null
  dev: boolean
  uploadEnabled: boolean
  // This tab has the session's audio; some tab has it.
  audioSink: boolean
  anySink: boolean
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
  // Navigator: topic chips shown while the overview plays, and the one open
  // prompt (choice | offer | start_choice | confirm_topic | table_choice) whose
  // options render as buttons; the free-text box answers any of them too.
  topics: { topic: string; section_id: string | null; heading: string | null }[]
  // The real section outline (title + the same est_minutes spoken at a
  // section transition), for an "up next" list. [] before the navigator has
  // run for this document -- nothing here is a client-side estimate.
  sections: { id: string; title: string; est_minutes: number | null }[]
  // Screen 3 ("Show script"): the per-clause heard/now/not_yet ledger,
  // fetched on demand (see session.ts getScript) -- never derived on the
  // client, since heard state is server-side and client-acknowledged only.
  script: { text: string; status: 'heard' | 'now' | 'not_yet' }[]
  prompt: Prompt | null
  heardAs: HeardAs | null
  // While a document is processed: the section list the structure pass found
  // (before enrichment) and the companion's lines, a muted running transcript.
  sectionsFound: string[]
  companionLines: { text: string; at: number }[]
  // Bumped by the welcome prompt's "upload" reply: the listener opens its dialog.
  focusUpload: number
  // The answer's own audio context, once it is sounding. Its rendered echoes
  // never touch the clause on screen.
  answerCtx: string | null
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
  uploadEnabled: false,
  audioSink: false,
  anySink: false,
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
  answerCtx: null,
  topics: [],
  sections: [],
  script: [],
  prompt: null,
  heardAs: null,
  sectionsFound: [],
  companionLines: [],
  focusUpload: 0,
  resume: null,
  error: null,
  status: {},
  contexts: [],
  metrics: null,
  events: [],
  ingestStages: [],
}

export type Prompt = {
  kind: 'choice' | 'offer' | 'start_choice' | 'confirm_topic' | 'table_choice'
    | 'welcome' | 'ingest_wait' | 'pick_topic' | 'section_end' | 'not_found' | 'end_choice' | string
  options: string[]
  text: string
  section_id?: string | null
  question?: string
  clause_id?: string
  labels?: string[]
  heading?: string
  topics?: string[]
  titles?: string[]
  next?: string
}

// What the server made of the last typed reply (reply_understood): shown
// under the question box until the next prompt opens.
export type HeardAs = { intent: string; sectionTitle: string | null; via: 'rules' | 'llm' | string }

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
export function buildInterrupt(contextId: string | null, renderedMs: number, audibleStopTs?: number): any[] {
  return [
    { type: 'flush_ack', context_id: contextId, rendered_ms: renderedMs, audible_stop_ts: audibleStopTs },
    { type: 'interrupt' },
  ]
}

/** Pause is an interrupt without a question: the same flush_ack first, so the
 *  server attributes the boundary to the clause being heard and re-reads it
 *  from the cursor instead of skipping it. */
export function buildPause(contextId: string | null, renderedMs: number, audibleStopTs?: number): any[] {
  return [
    { type: 'flush_ack', context_id: contextId, rendered_ms: renderedMs, audible_stop_ts: audibleStopTs },
    { type: 'pause' },
  ]
}

/** Opening another document while the voice is sounding is a cut too: the
 *  same flush ack first, so the clause being left is attributed at the
 *  playhead and the server never has to ask this socket for an ack it could
 *  not receive until this handler returned. */
/** A jump, a chip, or a spoken topic while the voice is sounding: the flush ack
 *  first, as for every other cut. The server then treats it as the
 *  interruption path with a different resume target. */
export function buildCut(msg: any, contextId: string | null, renderedMs: number, sounding: boolean, audibleStopTs?: number): any[] {
  if (!sounding) return [msg]
  return [{ type: 'flush_ack', context_id: contextId, rendered_ms: renderedMs, audible_stop_ts: audibleStopTs }, msg]
}

export function buildOpen(name: string, contextId: string | null, renderedMs: number, sounding: boolean, audibleStopTs?: number): any[] {
  if (!sounding) return [{ type: 'open', name }]
  return [
    { type: 'flush_ack', context_id: contextId, rendered_ms: renderedMs, audible_stop_ts: audibleStopTs },
    { type: 'open', name },
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
        uploadEnabled: action.value.upload_enabled ?? state.uploadEnabled,
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
      return { ...state, ingestStages: [], sectionsFound: [], companionLines: [] }
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
        uploadEnabled: !!m.upload_enabled,
        audioSink: !!m.sink,
        anySink: !!m.sink_any,
        provider: m.provider || {},
        providerFellBack: (m.provider?.provider ?? '') === 'fake',
        documents: m.documents || [],
        current: m.current ?? null,
        topics: m.topics && m.topics.length ? m.topics : state.topics,
        sections: m.sections && m.sections.length ? m.sections : state.sections,
      }

    case 'sink':
      return { ...state, audioSink: !!m.you, anySink: !!m.any }

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
        answerCtx: null,
        // The chips it carries, else the ones already showing for this same
        // document (a re-open never clears them), else none until they land.
        topics: m.topics && m.topics.length ? m.topics : m.name === state.current ? state.topics : [],
        sections: m.sections && m.sections.length ? m.sections : m.name === state.current ? state.sections : [],
        sectionsFound: m.name === state.current ? state.sectionsFound : [],
        script: m.name === state.current ? state.script : [],
        prompt: null,
        phase: 'idle',
      }

    case 'script':
      return { ...state, script: m.clauses || [] }

    case 'unit_started':
      if (m.kind === 'companion') {
        // The companion's line: heard, but never the clause on screen. It
        // joins the transcript under the progress bar.
        return {
          ...state,
          companionLines: [...state.companionLines, { text: m.text_display, at: Date.now() }].slice(-20),
        }
      }
      if (m.kind === 'answer') {
        // The spoken answer is a unit of its own so it is acked and interruptible
        // like a clause, but it is not document text: the clause on screen stays,
        // and only the phase changes. "Answering from the document…" is replaced
        // when the answer's audio actually starts, which is when this arrives.
        return { ...state, answerCtx: m.context_id, phase: 'speaking' }
      }
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
          charStart: m.char_start ?? 0,
        },
        words: null,
        spans: [],
        renderedMs: 0,
        // A new unit starts entirely unspoken. Nothing is marked said until the
        // audio clock says so.
        boundaryChar: m.char_start ?? 0,
        wordIndex: -1,
        answerCtx: null,
        phase: 'playing',
      }

    case 'timestamps':
      // The spoken answer has a word map too. It must not restyle the clause
      // on screen.
      if (m.context_id && state.unit && m.context_id !== state.unit.contextId) return state
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
        // Never below the resume point: the text before it was heard before
        // the cut, and the first ack of the remainder has no finished word yet.
        boundaryChar: state.phase === 'playing'
          ? Math.max(spoken, state.unit?.charStart ?? 0)
          : state.boundaryChar,
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
        phase: state.phase === 'answering' ? 'answering' : 'paused',
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
      // An interrupt's `paused` lands while the question is still in flight;
      // that must not clobber "Answering…". Once the answer text is here, a
      // `paused` means the spoken answer finished (or could not be spoken) and
      // the listener decides what happens next.
      return {
        ...state,
        phase: state.phase === 'answering' && !state.answer ? 'answering' : 'paused',
        answerCtx: null,
      }

    case 'answer':
      return {
        ...state,
        // The text arrives before its audio. Stay on "Answering…" until the
        // answer unit starts sounding; a replayed answer with no audio keeps
        // whatever phase it was in.
        phase: state.phase === 'answering' ? 'answering' : state.phase,
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

    case 'library_changed':
      return { ...state, documents: m.documents || state.documents }

    case 'document_deleted':
      // Gone from the library; if it was the open document the server says
      // which one is open now (or none).
      return {
        ...state,
        documents: m.documents || state.documents.filter((d) => d.name !== m.name),
        current: m.current !== undefined ? m.current : state.current === m.name ? null : state.current,
      }

    case 'sections_found':
      // The structure pass found the headings: the list shows before enrichment.
      return { ...state, sectionsFound: m.titles || [] }

    case 'navigator':
      // The overview and chips for a document are being generated on entry,
      // landed, or fell back to the mechanical map. Rows carry the state.
      return {
        ...state,
        documents: m.documents
          || state.documents.map((d) => (d.name === m.name ? { ...d, navigator: m.state } : d)),
        topics: m.state === 'ready' && m.topics && m.topics.length && m.name === state.current ? m.topics : state.topics,
        sections: m.state === 'ready' && m.sections && m.sections.length && m.name === state.current ? m.sections : state.sections,
      }

    case 'topics':
      if (m.document && state.current && m.document !== state.current) return state
      return { ...state, topics: m.topics || [] }

    case 'prompt':
      // The prompt was heard: its options are the listener's for a few seconds.
      return {
        ...state,
        prompt: {
          kind: m.kind, options: m.options || [], text: m.text || '',
          question: m.question, clause_id: m.clause_id, labels: m.labels, heading: m.heading,
          topics: m.topics, titles: m.titles, next: m.next,
        },
        heardAs: null,
        phase: m.kind === 'ingest_wait' ? state.phase : 'paused',
      }

    case 'reply_understood':
      return { ...state, heardAs: { intent: m.intent, sectionTitle: m.section_title ?? null, via: m.via } }

    case 'focus_upload':
      return { ...state, focusUpload: state.focusUpload + 1 }

    case 'prompt_closed':
      return { ...state, prompt: null }

    // The same two prompts under their older names, kept for older clients
    // and traces: they carry no more than the generic message does.
    case 'choice':
      return { ...state, prompt: { kind: 'choice', options: ['now', 'overview_first'], text: '',
                                   section_id: m.section_id ?? null }, phase: 'paused' }

    case 'choice_closed':
      return { ...state, prompt: state.prompt?.kind === 'choice' ? null : state.prompt }

    case 'offer':
      return { ...state, prompt: { kind: 'offer', options: ['yes', 'go_on'], text: '',
                                   question: m.question, clause_id: m.clause_id }, phase: 'paused' }

    case 'offer_closed':
      return { ...state, prompt: state.prompt?.kind === 'offer' ? null : state.prompt }

    case 'document_finished':
      return { ...state, phase: 'finished' }

    case 'jumped':
      return { ...state, answer: null, answerCtx: null, prompt: null, topics: [] }

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
