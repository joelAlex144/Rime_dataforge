import { fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import Listener from './Listener'
import Dev from './Dev'

/** Captured so tests can push server frames into the component. */
let sockets: MockSocket[] = []

class MockSocket {
  static OPEN = 1
  readyState = 1
  onopen: (() => void) | null = null
  onclose: (() => void) | null = null
  onmessage: ((e: { data: string }) => void) | null = null
  sent: any[] = []
  constructor(public url: string) {
    sockets.push(this)
    setTimeout(() => this.onopen?.(), 0)
  }
  send(s: string) {
    this.sent.push(JSON.parse(s))
  }
  close() {}
}

function feed(msg: any) {
  for (const s of sockets) s.onmessage?.({ data: JSON.stringify(msg) })
}

const STATUS = (dev: boolean, upload: boolean = dev) => ({
  session_id: 'web-abc123',
  dev,
  upload_enabled: upload,
  replaying: null,
  provider: { provider: 'fake', modelId: 'fake', speaker: 'fake', lang: 'en', audioFormat: 'pcm', samplingRate: 24000 },
  ingest: { state: dev ? 'ok' : 'off', detail: dev ? 'dev upload enabled' : 'build time only' },
  normalize: { state: 'ok', detail: 'ready' },
  rime_ws: { state: 'warn', detail: 'fake provider, no Rime connection' },
  client_ws: { state: 'ok', detail: '1 connected' },
  stt: { state: 'warn', detail: 'button only' },
  llm: { state: 'off', detail: 'extractive' },
})

const METRICS = {
  ttfb_p50: null, ttfb_p95: null, fenced_bytes_after_clear: null,
  interpolated_spans: null, spans_total: null, flush_ack_p50: null,
}

function mockFetch(dev: boolean, upload: boolean = dev) {
  return vi.fn(async (url: any, _init?: any) => {
    const u = String(url)
    const body =
      u.startsWith('/api/status') ? STATUS(dev, upload)
      : u.startsWith('/api/contexts') ? { contexts: [] }
      : u.startsWith('/api/metrics') ? METRICS
      : u.startsWith('/api/traces') ? { traces: [{ name: 'preflight.jsonl', bytes: 1226 }] }
      : {}
    return { ok: true, json: async () => body } as any
  })
}

const HELLO = {
  type: 'hello',
  session_id: 'web-abc123',
  dev: false,
  provider: { provider: 'fake' },
  current: 'policy',
  documents: [
    {
      name: 'policy', title: 'Northlake Mutual Homeowners Policy',
      section_count: 13, estimated_minutes: 22,
      progress: { started: true, finished: false, current_section_index: 4, minutes_left: 22 },
      referral: 'your insurer or lender',
    },
  ],
}

const UNIT = {
  type: 'unit_started',
  unit_id: 'sec-4b-vii',
  context_id: 'sec-4b-vii#t1',
  index: 46,
  section_title: 'Perils insured against',
  path: '4(b)',
  text_display: 'If any of the causes listed results in a sudden discharge of water we cover it.',
  sentences: [[0, 78]],
}

beforeEach(() => {
  sockets = []
  vi.stubGlobal('WebSocket', MockSocket as any)
})

afterEach(() => {
  vi.unstubAllGlobals()
  vi.restoreAllMocks()
})

describe('listener route', () => {
  beforeEach(() => vi.stubGlobal('fetch', mockFetch(false)))

  it('shows progress in words, not identifiers', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    expect(await screen.findByText(/Section 4 of 13/)).toBeInTheDocument()
    expect(screen.getByText(/22 min left/)).toBeInTheDocument()
  })

  it('renders no clause ids, no millisecond strings, and no provider name', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    feed(UNIT)
    feed({ type: 'timestamps', context_id: 'sec-4b-vii#t1', words: ['If'], start_ms: [0], end_ms: [180] })
    feed({ type: 'boundary', unit_id: 'sec-4b-vii', rendered_ms: 2560, char_end: 30, of: 78, word_index: 0 })
    await screen.findByText(/Paused here/)

    const text = document.body.textContent || ''
    expect(text).not.toMatch(/sec-\d+[a-z]?-[ivx]+/i)   // clause ids
    expect(text).not.toMatch(/\bt\d+\b/)                 // turn ids
    expect(text).not.toMatch(/\d+\s*ms\b/i)              // milliseconds
    expect(text).not.toMatch(/\brime\b|\bfake\b|\bcoda\b|\bbancroft\b/i)
    expect(text).not.toMatch(/contextId|char_end|rendered_ms/)
  })

  it('keeps text after the delivery boundary muted even though it was sent', async () => {
    const { container } = render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    feed(UNIT)
    feed({ type: 'boundary', unit_id: 'sec-4b-vii', rendered_ms: 2560, char_end: 30, of: 78, word_index: 0 })
    await screen.findByText(/Paused here/)
    const heard = container.querySelector('.heard')!
    const unheard = container.querySelector('.unheard')!
    expect(heard.textContent).toBe(UNIT.text_display.slice(0, 30))
    expect(unheard.textContent).toBe(UNIT.text_display.slice(30))
    expect(container.querySelector('.boundary')).toBeTruthy()
  })

  it('always renders the document-only disclaimer with the per-fixture referral', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    feed({
      type: 'answer', question: 'what does that mean', kind: 'in_scope', unit_id: 'sec-4b-vii',
      answer: 'The policy covers the resulting damage.', referral: 'your insurer or lender', offer: false,
    })
    const disclaimer = await screen.findByText(/Read from the document only/)
    // The referral is interpolated, so it lands in a sibling text node.
    expect(disclaimer.textContent).toMatch(/Read from the document only\./)
    expect(disclaimer.textContent).toMatch(/contact your insurer or lender\./)
  })

  it('offers jump or keep going only for a beyond-cursor answer', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    feed({ type: 'answer', question: 'q', kind: 'beyond_cursor', unit_id: 'sec-9a-i', answer: 'further down', referral: 'r', offer: true })
    expect(await screen.findByRole('button', { name: 'Jump there' })).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Keep going' })).toBeInTheDocument()
  })

  it('renders an eligibility refusal with no buttons', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    feed({
      type: 'answer', question: 'am I eligible', kind: 'eligibility', unit_id: 'sec-1-i',
      answer: "I can't tell you whether that applies to you.", referral: 'your insurer or lender', offer: false,
    })
    await screen.findByText(/can't tell you whether that applies/)
    expect(screen.queryByRole('button', { name: 'Jump there' })).toBeNull()
  })

  it('offers voice input, off by default, until the listener turns it on', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    const mic = await screen.findByRole('button', { name: 'Voice input' })
    expect(mic).not.toBeDisabled()
    expect(mic).toHaveAttribute('aria-pressed', 'false')
    expect(mic).toHaveAttribute('title', 'Talk instead of typing')
  })

  it('shows the fallback line and an amber dot when the provider flips to fake', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    feed({ type: 'provider_error', message: 'socket closed' })
    expect(await screen.findByText(/fallback voice/)).toBeInTheDocument()
    expect(document.querySelector('.dot.amber')).toBeTruthy()
    expect(document.body.textContent).not.toMatch(/Error:/)
  })

  it('reports the end of the document in plain words', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    feed({ type: 'document_finished', name: 'policy' })
    expect(await screen.findByText(/end of the document/)).toBeInTheDocument()
  })

  it('hides upload entirely when the server says upload is disabled', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)                                   // no upload_enabled
    await screen.findByText(/Section 4 of 13/)
    expect(document.querySelector('input[type="file"]')).toBeNull()
    expect(screen.queryByText('Add a document')).toBeNull()
  })

  it('offers Add a document, without an override control, when upload is enabled', async () => {
    // The status poll agrees with the hello: upload on, dev off.
    vi.stubGlobal('fetch', mockFetch(false, true))
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed({ ...HELLO, upload_enabled: true })
    await screen.findByText(/Section 4 of 13/)
    expect(screen.getByRole('button', { name: /Add a document/ })).toBeInTheDocument()
    expect(screen.getByLabelText('Document file')).toBeInTheDocument()
    expect(screen.queryByLabelText('Override reason')).toBeNull()
    expect(screen.getByText(/needs a person to review it first/)).toBeInTheDocument()
  })

  it('Enter while playing sends the interrupt sequence before the question', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    feed(UNIT)                                    // phase: playing
    await screen.findByText(/Reading section/)
    const input = screen.getByLabelText('Ask about what you just heard')
    fireEvent.change(input, { target: { value: 'what does that mean' } })
    fireEvent.submit(input.closest('form')!)
    const types = sockets[0].sent.map((m) => m.type)
    expect(types).toEqual(['flush_ack', 'interrupt', 'ask'])
    expect(sockets[0].sent[2].question).toBe('what does that mean')
    expect(screen.getByText(/Answering from the document/)).toBeInTheDocument()
  })

  it('Enter while paused asks without an interrupt', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    await screen.findByText(/Section 4 of 13/)
    const input = screen.getByLabelText('Ask about what you just heard')
    fireEvent.change(input, { target: { value: 'what is the deductible' } })
    fireEvent.submit(input.closest('form')!)
    expect(sockets[0].sent.map((m) => m.type)).toEqual(['ask'])
  })

  it('Keep going sends play, not just a dismissal', async () => {
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed(HELLO)
    feed({ type: 'answer', question: 'q', kind: 'beyond_cursor', unit_id: 'sec-7a-i', answer: 'a',
           referral: 'your insurer or lender', offer: true })
    fireEvent.click(await screen.findByText('Keep going'))
    expect(sockets[0].sent.map((m) => m.type)).toEqual(['play'])
  })
})

describe('dev route', () => {
  it('hides the drop zone and explains why when upload is disabled', async () => {
    vi.stubGlobal('fetch', mockFetch(false))
    render(<MemoryRouter><Dev /></MemoryRouter>)
    expect(await screen.findByText(/Upload is disabled/)).toBeInTheDocument()
    expect(document.querySelector('input[type="file"]')).toBeNull()
    expect(document.querySelector('.drop')).toBeNull()
  })

  it('shows the drop zone with the override control when upload is enabled', async () => {
    vi.stubGlobal('fetch', mockFetch(true))
    render(<MemoryRouter><Dev /></MemoryRouter>)
    await waitFor(() => expect(document.querySelector('.drop')).toBeTruthy())
    expect(screen.getByLabelText('Document file')).toBeInTheDocument()
    expect(screen.getByLabelText('Provider')).toBeInTheDocument()
  })

  it('renders the six status cells with their detail strings', async () => {
    vi.stubGlobal('fetch', mockFetch(false))
    render(<MemoryRouter><Dev /></MemoryRouter>)
    for (const k of ['ingest', 'normalize', 'rime ws3', 'client ws', 'stt', 'llm']) {
      expect(await screen.findByText(k)).toBeInTheDocument()
    }
    expect(await screen.findByText(/button only/)).toBeInTheDocument()
    expect(screen.getByText(/extractive/)).toBeInTheDocument()
  })

  it('shows n/a for metrics that are null, and never invents far end', async () => {
    vi.stubGlobal('fetch', mockFetch(false))
    render(<MemoryRouter><Dev /></MemoryRouter>)
    await waitFor(() => expect(screen.getAllByText('n/a').length).toBeGreaterThan(2))
    expect(screen.getByText(/far end n\/a/)).toBeInTheDocument()
  })

  it('replay populates the events list', async () => {
    vi.stubGlobal('fetch', mockFetch(false))
    render(<MemoryRouter><Dev /></MemoryRouter>)
    await screen.findByText('ingest')
    feed({ type: 'replay_start', trace: 'preflight.jsonl' })
    feed({ type: 'event', record: { ts_ms: 1000, type: 'unit_truncated', context_id: 'sec-4b-vii', char_end: 30, of: 349 } })
    feed({ type: 'event', record: { ts_ms: 1100, type: 'result_fenced', context_id: 'sec-4b-vii', bytes: 4096 } })

    const list = await screen.findByTestId('event-list')
    await waitFor(() => expect(within(list).getByText('unit_truncated')).toBeInTheDocument())
    expect(within(list).getByText('result_fenced')).toBeInTheDocument()
    expect(await screen.findByText(/Replaying preflight.jsonl/)).toBeInTheDocument()
    expect(screen.getByText(/2 records/)).toBeInTheDocument()
  })

  it('shows the boundary row from a real event, not a guess', async () => {
    vi.stubGlobal('fetch', mockFetch(false))
    render(<MemoryRouter><Dev /></MemoryRouter>)
    await screen.findByText('ingest')
    feed({ type: 'event', record: { type: 'unit_truncated', context_id: 'sec-4b-vii', rendered_ms: 2560, char_end: 30, of: 349 } })
    expect(await screen.findByText(/sec-4b-vii · 2560 ms -> char 30 of 349/)).toBeInTheDocument()
  })

  it('has play, pause and stop-and-ask, and says which tab has the voice', async () => {
    vi.stubGlobal('fetch', mockFetch(false))
    render(<MemoryRouter><Dev /></MemoryRouter>)
    feed({ ...HELLO, sink: false, sink_any: true })
    expect(await screen.findByText('voice: another tab')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Play' }))
    expect(sockets[0].sent.map((m) => m.type)).toEqual(['play'])
    feed(UNIT)                                        // playing
    expect(await screen.findByRole('button', { name: 'Pause' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Stop and ask' }))
    expect(sockets[0].sent.map((m) => m.type)).toEqual(['play', 'flush_ack', 'interrupt'])
    feed({ type: 'sink', you: true, any: true })
    expect(await screen.findByText('voice: this tab')).toBeInTheDocument()
  })

  it('a tab without the voice shows each clause as it starts', async () => {
    vi.stubGlobal('fetch', mockFetch(false))
    render(<MemoryRouter><Listener /></MemoryRouter>)
    feed({ ...HELLO, sink: false, sink_any: true })
    feed(UNIT)
    feed({ ...UNIT, unit_id: 'sec-4b-viii', context_id: 'sec-4b-viii#t2', index: 47,
           text_display: 'A second clause that arrives while the first is still sounding elsewhere.' })
    expect(await screen.findByText(/A second clause that arrives/)).toBeInTheDocument()
    expect(screen.getByText(/playing in another tab/)).toBeInTheDocument()
  })

  it('a successful upload on /dev opens the document at once and names it', async () => {
    const fetchMock = mockFetch(true)
    vi.stubGlobal('fetch', fetchMock)
    class FakeXHR {
      static last: any = null
      upload: any = { onprogress: null }
      onload: any = null
      onerror: any = null
      status = 0
      responseText = ''
      open() {}
      setRequestHeader() {}
      send() { FakeXHR.last = this }
    }
    vi.stubGlobal('XMLHttpRequest', FakeXHR as any)
    render(<MemoryRouter><Dev /></MemoryRouter>)
    feed(HELLO)
    await waitFor(() => expect(document.querySelector('.drop')).toBeTruthy())
    fireEvent.change(screen.getByLabelText('Document file'), {
      target: { files: [new File([new Uint8Array(64)], 'wording.pdf', { type: 'application/pdf' })] },
    })
    FakeXHR.last.status = 200
    FakeXHR.last.responseText = JSON.stringify({
      ok: true, name: 'wording', clause_count: 25, path: 'fixtures/unreviewed/wording.json',
      report: [], warnings: [], preview: [], note: 'Written to fixtures/unreviewed/.',
    })
    FakeXHR.last.onload()
    await waitFor(() =>
      expect(fetchMock.mock.calls.some(([u]) => String(u).startsWith('/api/dev/open?unreviewed=1'))).toBe(true))
    const call = fetchMock.mock.calls.find(([u]) => String(u).startsWith('/api/dev/open'))!
    expect(JSON.parse((call[1] as any).body).name).toBe('wording')
    feed({ type: 'document_opened', name: 'wording', documents: [] })
    expect(await screen.findByText(/document: wording/)).toBeInTheDocument()
    expect(screen.getByText(/wording \(open\)/)).toBeInTheDocument()
  })

  it('identifiers are expected here, unlike the listener', async () => {
    vi.stubGlobal('fetch', mockFetch(false))
    render(<MemoryRouter><Dev /></MemoryRouter>)
    expect(await screen.findByText(/web-abc123/)).toBeInTheDocument()
    expect(screen.getByText(/provider=fake/)).toBeInTheDocument()
  })
})
