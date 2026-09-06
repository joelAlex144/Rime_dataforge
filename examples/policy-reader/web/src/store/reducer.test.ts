import { describe, expect, it } from 'vitest'
import {
  Action,
  State,
  buildInterrupt,
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

describe('upload gate', () => {
  it('hello carries upload_enabled', () => {
    expect(run([{ type: 'hello', upload_enabled: true, documents: [] }]).uploadEnabled).toBe(true)
    expect(run([{ type: 'hello', documents: [] }]).uploadEnabled).toBe(false)
  })
  it('status carries it too, for /dev', () => {
    const s = reducer(initialState, { type: 'status', value: { upload_enabled: true } as any })
    expect(s.uploadEnabled).toBe(true)
  })
})
