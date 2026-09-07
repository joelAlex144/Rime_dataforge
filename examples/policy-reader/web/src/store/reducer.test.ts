import { describe, expect, it } from 'vitest'
import {
  Action,
  State,
  buildInterrupt,
  buildOpen,
  buildPause,
  initialState,
  reducer,
  spokenCharsAt,
  wordAt,
} from './reducer'

const srv = (msg: any): Action => ({ type: 'server', msg })

function run(msgs: any[], from: State = initialState): State {
  return msgs.reduce((s, m) => reducer(s, srv(m)), from)
}

const UNIT = {
  type: 'unit_started',
  unit_id: 'sec-4b-vii',
  context_id: 'sec-4b-vii#t1',
  index: 46,
  section_title: 'Perils insured against',
  path: '4(b)(vii)',
  text_display: 'If any of the causes listed in Section 4(b)(i) results in a discharge of water.',
  sentences: [[0, 78]],
}

const WORDS = {
  type: 'timestamps',
  context_id: 'sec-4b-vii#t1',
  words: ['If', 'any', 'of', 'the', 'causes'],
  start_ms: [0, 180, 360, 540, 720],
  end_ms: [180, 360, 540, 720, 900],
}

describe('rendered acks drive position', () => {
  it('a new unit starts entirely unheard', () => {
    const s = run([UNIT])
    expect(s.boundaryChar).toBe(0)
    expect(s.phase).toBe('playing')
  })

  it('rendered advances the playing word from the audio clock', () => {
    const s = run([UNIT, WORDS, { type: 'rendered', rendered_ms: 570 }])
    expect(s.renderedMs).toBe(570)
    expect(s.wordIndex).toBe(3)
    expect(WORDS.words[s.wordIndex]).toBe('the')
  })

  it('boundary sets the delivery char and pauses', () => {
    const s = run([
      UNIT,
      WORDS,
      { type: 'rendered', rendered_ms: 540 },
      { type: 'boundary', unit_id: 'sec-4b-vii', rendered_ms: 540, char_end: 30, of: 78, word_index: 3 },
    ])
    expect(s.boundaryChar).toBe(30)
    expect(s.phase).toBe('paused')
    expect(s.heardUnits['sec-4b-vii']).toBe('truncated@30')
  })

  it('wordAt returns -1 before the first word sounds', () => {
    expect(wordAt({ words: ['a'], start: [100], end: [200] }, 0)).toBe(-1)
  })
})

describe('rendered echoes are scoped to the clause on screen', () => {
  it('an echo for the displayed clause advances the read-along', () => {
    const s = run([
      UNIT,
      { type: 'timestamps', context_id: UNIT.context_id, words: WORDS.words,
        start_ms: WORDS.start_ms, end_ms: WORDS.end_ms,
        spans: [{ char_start: 0, char_end: 2, t_start_ms: 0, t_end_ms: 180 },
                { char_start: 3, char_end: 6, t_start_ms: 180, t_end_ms: 360 }] },
      { type: 'rendered', rendered_ms: 200, context_id: UNIT.context_id },
    ])
    expect(s.renderedMs).toBe(200)
    expect(s.boundaryChar).toBe(2)
  })

  it('an echo for a different clause is ignored', () => {
    // The queue is continuous, so acks can be produced for a unit whose audio
    // is buffered but not yet on screen. Applying one would advance the
    // highlight of the clause the listener is still hearing.
    const s = run([
      UNIT,
      { type: 'rendered', rendered_ms: 9999, context_id: 'some-other-unit#t9' },
    ])
    expect(s.renderedMs).toBe(0)
    expect(s.boundaryChar).toBe(0)
  })

  it('an echo with no context is still applied, for the single-unit case', () => {
    const s = run([UNIT, { type: 'rendered', rendered_ms: 120 }])
    expect(s.renderedMs).toBe(120)
  })
})

describe('read-along boundary', () => {
  it('spokenCharsAt does not count a half-spoken word', () => {
    const spans = [
      { char_start: 0, char_end: 5, t_start_ms: 0, t_end_ms: 300 },
      { char_start: 6, char_end: 10, t_start_ms: 300, t_end_ms: 800 },
    ]
    expect(spokenCharsAt(spans, 500)).toBe(5)
    expect(spokenCharsAt(spans, 800)).toBe(10)
    expect(spokenCharsAt(spans, 0)).toBe(0)
  })
})

describe('heard is never inferred from sent', () => {
  it('audio chunks arriving do not move the boundary', () => {
    const s = run([UNIT, WORDS, { type: 'audio', context_id: 'x', seq: 0, b64: '' }])
    expect(s.boundaryChar).toBe(0)
    expect(s.heardUnits['sec-4b-vii']).toBeUndefined()
  })

  it('unit_done alone does not mark the unit heard', () => {
    // unit_done means the server finished SENDING. The ledger must wait for acks.
    const s = run([UNIT, { type: 'unit_done', context_id: 'sec-4b-vii#t1', unit_id: 'sec-4b-vii' }])
    expect(s.heardUnits['sec-4b-vii']).toBeUndefined()
  })

  it('unit_heard, which the server emits only after acks, marks it heard', () => {
    const s = run([
      UNIT,
      { type: 'unit_done', context_id: 'sec-4b-vii#t1', unit_id: 'sec-4b-vii' },
      { type: 'unit_heard', unit_id: 'sec-4b-vii', char_end: 78 },
    ])
    expect(s.heardUnits['sec-4b-vii']).toBe('heard')
    expect(s.boundaryChar).toBe(78)
  })
})

describe('interrupt ordering', () => {
  it('flush ack goes out before the interrupt', () => {
    const msgs = buildInterrupt('sec-4b-vii#t1', 2560)
    expect(msgs.map((m) => m.type)).toEqual(['flush_ack', 'interrupt'])
  })

  it('the flush ack carries the position the audio clock reached', () => {
    const [ack] = buildInterrupt('sec-4b-vii#t1', 2560)
    expect(ack.rendered_ms).toBe(2560)
    expect(ack.context_id).toBe('sec-4b-vii#t1')
  })

  it('the interrupt itself carries no position', () => {
    const [, stop] = buildInterrupt('c', 1)
    expect(Object.keys(stop)).toEqual(['type'])
  })
})

describe('pause ordering', () => {
  it('pause sends the same flush ack first, then pause', () => {
    // Without the ack the server had no playhead to attribute the stop to and
    // left the cursor past the paused clause: sec-3-p4 was skipped after a
    // pause at 0.9 s in traces/session_web-29e08c00.jsonl.
    const msgs = buildPause('sec-3-p4#t5', 906.7)
    expect(msgs.map((m) => m.type)).toEqual(['flush_ack', 'pause'])
    expect(msgs[0].rendered_ms).toBe(906.7)
    expect(msgs[0].context_id).toBe('sec-3-p4#t5')
    expect(Object.keys(msgs[1])).toEqual(['type'])
  })
})

describe('provider flip', () => {
  it('a fake provider raises the fallback flag', () => {
    const s = run([{ type: 'provider_active', provider: 'fake', modelId: 'fake' }])
    expect(s.providerFellBack).toBe(true)
    expect(s.provider.provider).toBe('fake')
  })

  it('rime clears it', () => {
    const s = run([
      { type: 'provider_active', provider: 'fake' },
      { type: 'provider_active', provider: 'rime', modelId: 'coda', speaker: 'bancroft' },
    ])
    expect(s.providerFellBack).toBe(false)
    expect(s.provider.speaker).toBe('bancroft')
  })

  it('a provider error sets the listener-facing copy, with no first person', () => {
    const s = run([{ type: 'provider_error', message: 'boom' }])
    expect(s.error).toMatch(/fallback voice/)
    expect(s.error).not.toMatch(/\bI\b|Error:/)
  })

  it('a provider_active event record also flips the badge during replay', () => {
    const s = reducer(initialState, srv({ type: 'event', record: { type: 'provider_active', provider: 'fake' } }))
    expect(s.providerFellBack).toBe(true)
  })
})

describe('answers', () => {
  it('an answer always carries a referral', () => {
    const s = run([
      { type: 'answer', question: 'q', kind: 'in_scope', unit_id: 'u', answer: 'a', referral: 'your insurer', offer: false },
    ])
    expect(s.answer?.referral).toBe('your insurer')
  })

  it('beyond_cursor sets the offer flag so the jump buttons render', () => {
    const s = run([{ type: 'answer', question: 'q', kind: 'beyond_cursor', answer: 'a', referral: 'r', offer: true }])
    expect(s.answer?.offer).toBe(true)
  })
})

describe('events and replay', () => {
  it('replay_start clears the list and records the trace name', () => {
    let s = reducer(initialState, srv({ type: 'event', record: { type: 'old' } }))
    s = reducer(s, srv({ type: 'replay_start', trace: 't.jsonl' }))
    expect(s.replaying).toBe('t.jsonl')
    expect(s.events).toHaveLength(0)
  })

  it('replayed records populate the event list and the ledger', () => {
    let s = reducer(initialState, srv({ type: 'replay_start', trace: 't.jsonl' }))
    s = reducer(s, srv({ type: 'event', record: { type: 'unit_truncated', context_id: 'sec-1-i', char_end: 30 } }))
    s = reducer(s, srv({ type: 'replay_end', trace: 't.jsonl' }))
    expect(s.events).toHaveLength(1)
    expect(s.heardUnits['sec-1-i']).toBe('truncated@30')
    expect(s.replaying).toBeNull()
  })

  it('the event list is capped at 500 rows', () => {
    let s = initialState
    for (let i = 0; i < 640; i++) s = reducer(s, srv({ type: 'event', record: { type: 't', i } }))
    expect(s.events).toHaveLength(500)
    expect((s.events[s.events.length - 1] as any).i).toBe(639)
  })
})

describe('ingest progress', () => {
  it('stages accumulate in order', () => {
    const s = run([
      { type: 'ingest_progress', stage: 'extract', state: 'ok', detail: 'read' },
      { type: 'ingest_progress', stage: 'pii scan', state: 'fail', detail: 'refused' },
    ])
    expect(s.ingestStages.map((x) => x.stage)).toEqual(['extract', 'pii scan'])
    expect(s.ingestStages[1].state).toBe('fail')
  })
})

describe('spoken answers', () => {
  const ASK = { type: 'askPending', question: 'q' } as const
  it('the answer text keeps "answering" until its audio starts', () => {
    // Enter while playing: askPending lands before the interrupt's boundary
    // and paused come back from the server.
    let s = run([UNIT])
    s = reducer(s, ASK)
    s = run([{ type: 'paused' }], s)                // the interrupt's paused
    expect(s.phase).toBe('answering')
    s = run([{ type: 'boundary', unit_id: 'sec-4b-vii', rendered_ms: 300, char_end: 20, of: 78 }], s)
    expect(s.phase).toBe('answering')
    s = run([{ type: 'answer', question: 'q', kind: 'in_scope', unit_id: 'u', answer: 'a', referral: 'r', offer: false }], s)
    expect(s.phase).toBe('answering')
    expect(s.answer?.answer).toBe('a')
  })

  it('an answer unit starting means speaking, without replacing the clause on screen', () => {
    let s = run([UNIT])
    s = reducer(s, ASK)
    s = run([{ type: 'unit_started', kind: 'answer', context_id: 'answer#t3', unit_id: 'answer',
               index: -1, section_title: 'Answer', text_display: 'the answer', sentences: [[0, 10]] }], s)
    expect(s.phase).toBe('speaking')
    expect(s.answerCtx).toBe('answer#t3')
    expect(s.unit?.unitId).toBe('sec-4b-vii')
    expect(s.unit?.textDisplay).toBe(UNIT.text_display)
  })

  it('the answer\'s timestamps and acks never touch the clause on screen', () => {
    let s = run([UNIT, WORDS])
    s = run([{ type: 'timestamps', context_id: 'answer#t3', words: ['x'], start_ms: [0], end_ms: [100],
               spans: [{ char_start: 0, char_end: 1, t_start_ms: 0, t_end_ms: 100 }] }], s)
    expect(s.words?.words).toEqual(WORDS.words)
    s = run([{ type: 'rendered', rendered_ms: 5000, context_id: 'answer#t3' }], s)
    expect(s.renderedMs).toBe(0)
  })

  it('paused after the answer text is a real pause (Jump there / Keep going)', () => {
    let s = reducer(initialState, ASK)
    s = run([{ type: 'answer', question: 'q', kind: 'beyond_cursor', answer: 'a', referral: 'r', offer: true },
             { type: 'paused' }], s)
    expect(s.phase).toBe('paused')
    expect(s.answer?.offer).toBe(true)
  })
})

describe('resumed clause', () => {
  it('starts with the already-heard text as ink and never drops below it on the first ack', () => {
    let s = run([{ ...UNIT, char_start: 40 }])
    expect(s.boundaryChar).toBe(40)
    s = run([{ type: 'timestamps', context_id: UNIT.context_id, words: ['If'], start_ms: [0], end_ms: [180],
               spans: [{ char_start: 40, char_end: 42, t_start_ms: 0, t_end_ms: 180 }] },
             { type: 'rendered', rendered_ms: 50, context_id: UNIT.context_id }], s)
    expect(s.boundaryChar).toBe(40)
    s = run([{ type: 'rendered', rendered_ms: 200, context_id: UNIT.context_id }], s)
    expect(s.boundaryChar).toBe(42)
  })
})

describe('library', () => {
  it('library_changed replaces the document list', () => {
    const s = run([{ type: 'hello', documents: [{ name: 'a' }] }, { type: 'library_changed', documents: [{ name: 'a' }, { name: 'b', reviewed: false }] }])
    expect(s.documents.map((d) => d.name)).toEqual(['a', 'b'])
  })
})

describe('audio sink', () => {
  it('hello and sink messages say whether this tab has the voice', () => {
    let s = run([{ type: 'hello', documents: [], sink: false, sink_any: true }])
    expect(s.audioSink).toBe(false)
    expect(s.anySink).toBe(true)
    s = run([{ type: 'sink', you: true, any: true }], s)
    expect(s.audioSink).toBe(true)
    s = run([{ type: 'sink', you: false, any: false }], s)
    expect(s.anySink).toBe(false)
  })
})

describe('open ordering', () => {
  it('open while sounding sends the flush ack first', () => {
    expect(buildOpen('carers', 'sec-1-i#t3', 1234, true).map((m) => m.type)).toEqual(['flush_ack', 'open'])
    expect(buildOpen('carers', 'sec-1-i#t3', 1234, true)[0].rendered_ms).toBe(1234)
  })
  it('open while silent is just open', () => {
    expect(buildOpen('carers', null, 0, false)).toEqual([{ type: 'open', name: 'carers' }])
  })
})


describe('prompts', () => {
  it('a prompt message opens the one prompt with its options; prompt_closed clears it', () => {
    let s = run([{ type: 'prompt', kind: 'table_choice', options: ['row', 'all', 'carry_on'], text: 'Here there is a table',
                   labels: ['Document type', 'Issuer'] }])
    expect(s.prompt?.kind).toBe('table_choice')
    expect(s.prompt?.labels).toEqual(['Document type', 'Issuer'])
    expect(s.phase).toBe('paused')
    s = run([{ type: 'prompt', kind: 'start_choice', options: ['topic', 'brief', 'start'], text: 'I have gone through' },
             { type: 'prompt_closed', kind: 'start_choice' }])
    expect(s.prompt).toBeNull()
  })

  it('the older choice and offer messages feed the same prompt state', () => {
    let s = run([{ type: 'choice', options: ['now', 'overview_first'], section_id: 'sec-4' }])
    expect(s.prompt).toEqual({ kind: 'choice', options: ['now', 'overview_first'], text: '', section_id: 'sec-4' })
    s = run([{ type: 'offer', question: 'q?', clause_id: 'c1' }, { type: 'offer_closed' }])
    expect(s.prompt).toBeNull()
    s = run([{ type: 'offer', question: 'q?', clause_id: 'c1' }, { type: 'jumped', unit_id: 'c9' }])
    expect(s.prompt).toBeNull()
  })
})

describe('understanding and the welcome upload', () => {
  it('reply_understood shows under the box until the next prompt opens', () => {
    let s = run([{ type: 'reply_understood', intent: 'topic', section_id: 'sec-4', section_title: 'General Exclusions', via: 'llm' }])
    expect(s.heardAs).toEqual({ intent: 'topic', sectionTitle: 'General Exclusions', via: 'llm' })
    s = run([{ type: 'prompt', kind: 'section_end', options: ['carry_on', 'topic', 'question'], text: 'That is Declarations.', next: 'Coverages' }], s)
    expect(s.heardAs).toBeNull()
    expect(s.prompt?.next).toBe('Coverages')
  })

  it('focus_upload bumps the counter the listener watches; ingest_wait keeps the phase', () => {
    let s = run([{ type: 'focus_upload' }])
    expect(s.focusUpload).toBe(1)
    s = run([{ type: 'prompt', kind: 'ingest_wait', options: ['question'], text: 'I am going through the document now' }], { ...initialState, phase: 'idle' })
    expect(s.prompt?.kind).toBe('ingest_wait')
    expect(s.phase).toBe('idle')
  })
})

describe('the companion while a document is processed', () => {
  it('document_opened keeps the chips of the document already open, takes the chips it carries, clears for another', () => {
    let s = run([
      { type: 'hello', documents: [{ name: 'a' }, { name: 'b' }], current: 'a' },
      { type: 'topics', document: 'a', topics: [{ topic: 'Exclusions', section_id: 's1', heading: 'Exclusions' }] },
    ])
    expect(s.topics.map((t) => t.topic)).toEqual(['Exclusions'])
    s = run([{ type: 'document_opened', name: 'a', documents: [{ name: 'a' }, { name: 'b' }] }], s)
    expect(s.topics.map((t) => t.topic)).toEqual(['Exclusions'])
    s = run([{ type: 'document_opened', name: 'b', documents: [{ name: 'a' }, { name: 'b' }],
              topics: [{ topic: 'Premium', section_id: 's2', heading: 'Premium' }] }], s)
    expect(s.topics.map((t) => t.topic)).toEqual(['Premium'])
    s = run([{ type: 'document_opened', name: 'a', documents: [{ name: 'a' }, { name: 'b' }] }], s)
    expect(s.topics).toEqual([])
    // chips for a document that is not the one open are ignored
    s = run([{ type: 'topics', document: 'b', topics: [{ topic: 'X', section_id: 's3', heading: 'X' }] }], s)
    expect(s.topics).toEqual([])
  })

  it('a spoken prompt never replaces the clause on screen: unit_started clause -> table_choice -> paused', () => {
    let s = run([UNIT, { type: 'rendered', context_id: UNIT.context_id, rendered_ms: 400 }])
    const before = s.boundaryChar
    s = run([{
      type: 'unit_started', kind: 'table_choice', unit_id: 'sec-1-t1', context_id: 'table_choice#t2', index: 3,
      section_title: 'Section 1', path: null,
      text_display: "Here there's a table of Document type to Source URL, 8 rows: Document type, Issuer / source. Want one of them, all of them, or shall I carry on?",
      sentences: [[0, 60]], char_start: 0,
    }], s)
    s = run([{ type: 'paused' }], s)
    expect(s.unit?.textDisplay).toBe(UNIT.text_display)
    expect(s.unit?.contextId).toBe(UNIT.context_id)
    expect(s.boundaryChar).toBe(before)
    expect(s.phase).toBe('paused')
    // the prompt's words are the prompt's, not the clause's
    expect(s.prompt?.text).toMatch(/^Here there's a table/)
    expect(s.unit?.textDisplay).not.toMatch(/table/)
  })

  it('every non-clause kind leaves the clause alone; clause and row set it', () => {
    let s = run([UNIT, { type: 'paused' }])                 // the clause on screen, paused
    for (const kind of ['map', 'cue', 'welcome', 'start_choice', 'pick_topic', 'confirm_topic', 'choice', 'offer',
                        'section_end', 'not_found', 'end_choice', 'recap']) {
      const next = run([{ type: 'unit_started', kind, unit_id: kind, context_id: `${kind}#t9`, index: -1,
                          section_title: 'x', text_display: `spoken ${kind}`, sentences: [[0, 5]] }], s)
      expect(next.unit?.contextId).toBe(UNIT.context_id)
      expect(next.unit?.textDisplay).toBe(UNIT.text_display)
      expect(next.phase).toBe('paused')                    // never set playing by a prompt
    }
    const row = run([{ type: 'unit_started', kind: 'row', unit_id: 'sec-1-t1-r2', context_id: 'row#t3', index: 4,
                       section_title: 'Section 1', text_display: 'Issuer / source: Reliance General.', sentences: [[0, 30]] }], s)
    expect(row.unit?.unitId).toBe('sec-1-t1-r2')
    expect(row.phase).toBe('playing')
  })

  it('sections_found fills the list and a companion unit joins the transcript, not the clause on screen', () => {
    let s = run([{ type: 'sections_found', titles: ['Definitions', 'Premium'] }])
    expect(s.sectionsFound).toEqual(['Definitions', 'Premium'])
    s = run([{ type: 'unit_started', context_id: 'companion#t1', unit_id: 'companion', index: -1, kind: 'companion',
               section_title: 'Doc', text_display: 'Got the text, 12 pages.', sentences: [[0, 23]], char_start: 0 }], s)
    expect(s.companionLines.map((l) => l.text)).toEqual(['Got the text, 12 pages.'])
    expect(s.unit).toBeNull()
  })

  it('clearIngest resets both for the next upload', () => {
    const s = run([{ type: 'sections_found', titles: ['A'] }])
    expect(reducer(s, { type: 'clearIngest' }).sectionsFound).toEqual([])
    expect(reducer(s, { type: 'clearIngest' }).companionLines).toEqual([])
  })
})
