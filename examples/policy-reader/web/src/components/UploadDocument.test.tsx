import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import UploadDocument, { parseSse } from './UploadDocument'

/** XMLHttpRequest that streams a server-sent-event body the way the server does. */
class FakeXHR {
  static last: FakeXHR | null = null
  upload: { onprogress: ((e: any) => void) | null } = { onprogress: null }
  onprogress: (() => void) | null = null
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  status = 0
  responseText = ''
  body: any = null
  open() {}
  setRequestHeader() {}
  send(body: any) {
    this.body = body
    FakeXHR.last = this
  }
  uploaded(loaded: number, total: number) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total })
  }
  frame(ev: any) {
    this.status = 200
    this.responseText += `data: ${JSON.stringify(ev)}\n\n`
    this.onprogress?.()
  }
  finish() {
    this.onload?.()
  }
  fail(status: number, text: string) {
    this.status = status
    this.responseText = text
    this.onload?.()
  }
}

const ENTRY = { doc_id: 'abc123def4567890', name: 'abc123def4567890', title: 'wording', reviewed: false, readable: true, clause_count: 180 }
const REPORT = {
  doc_id: ENTRY.doc_id, name: ENTRY.name, title: 'wording', elapsed_ms: { total: 3100 },
  pii_scan: { personal: [{ label: 'email', redacted: 'hema…', clause_id: 'sec-13-p2' }],
              institutional: [{ label: 'phone', redacted: '1800…', clause_id: 'sec-13-p1' }], note: 'Informational.' },
  validate: { clause_count: 180, by_kind: { body: 141, heading: 38, table_stub: 1 }, body_clauses: 141, readable: true,
              splits: ['split sec-2-p2: 618 chars -> 2 pieces (431, 186)'], folds_and_merges: ['folded 40 list items into their runs (max 600 chars)'],
              oversized_ok: true, max_clause_chars: 912,
              boilerplate_first_15: [{ id: 'sec-1-b1', text: 'Page 1 of 9', reason: 'page_number' }],
              expected_range: { reference: '150-250 clauses for a ~12,000-word wording', words: 4300, scaled_range: [54, 90], in_range: true },
              other_notes: [], warnings: [] },
}
const file = () => new File([new Uint8Array(2048)], 'wording.pdf', { type: 'application/pdf' })

beforeEach(() => {
  FakeXHR.last = null
  vi.stubGlobal('XMLHttpRequest', FakeXHR as any)
})
afterEach(() => vi.unstubAllGlobals())

async function runToDone(xhr: FakeXHR) {
  await act(async () => xhr.uploaded(2048, 2048))
  for (const st of ['extract', 'structure', 'segment', 'normalize', 'pii_scan', 'validate', 'write']) {
    await act(async () => xhr.frame({ stage: st, status: 'ok', elapsed_ms: 10, detail: `${st} detail` }))
  }
  await act(async () => {
    xhr.frame({ stage: 'done', status: 'ok', elapsed_ms: 3100, entry: ENTRY })
    xhr.finish()
  })
}

describe('parseSse', () => {
  it('yields complete frames and reports how far it consumed', () => {
    const text = 'data: {"stage":"extract","status":"ok","elapsed_ms":1}\n\ndata: {"stage":"str'
    const r = parseSse(text, 0)
    expect(r.events.map((e) => e.stage)).toEqual(['extract'])
    expect(text.slice(r.consumed)).toBe('data: {"stage":"str')
  })
})

describe('listener face', () => {
  it('shows only a progress bar and elapsed time, then the title; never the report', async () => {
    const onDone = vi.fn()
    render(<UploadDocument face="listener" onDone={onDone} />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    const xhr = FakeXHR.last!
    expect(screen.getByRole('progressbar')).toBeInTheDocument()
    await runToDone(xhr)
    expect(screen.getByText(/wording/)).toBeInTheDocument()
    expect(onDone).toHaveBeenCalledWith(expect.objectContaining({ name: ENTRY.name }))
    // No stage names, no counts, no scan findings, no accept step.
    for (const s of ['extract', 'pii_scan', 'validate', 'Accept', '180 clauses', 'hema', 'Page 1 of 9']) {
      expect(screen.queryByText(new RegExp(s))).toBeNull()
    }
    expect(screen.queryByLabelText('Document URL')).toBeNull()
  })

  it('says when no readable text was found', async () => {
    render(<UploadDocument face="listener" />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    const xhr = FakeXHR.last!
    await act(async () => {
      xhr.frame({ stage: 'done', status: 'ok', elapsed_ms: 5, entry: { ...ENTRY, readable: false } })
      xhr.finish()
    })
    expect(screen.getByText(/No readable text found/)).toBeInTheDocument()
  })

  it('surfaces the two HTTP errors verbatim', async () => {
    render(<UploadDocument face="listener" />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    await act(async () => FakeXHR.last!.fail(415, JSON.stringify({ error: "unsupported type '.exe'" })))
    expect(screen.getByText(/unsupported type/)).toBeInTheDocument()
  })

  it('a document whose text is already here is a 409 with the companion line', async () => {
    render(<UploadDocument face="listener" />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    await act(async () =>
      FakeXHR.last!.fail(409, JSON.stringify({ error: 'That looks like Arogya Sanjeevani, already here.', existing: ENTRY })),
    )
    expect(screen.getByText(/That looks like Arogya Sanjeevani, already here\./)).toBeInTheDocument()
  })
})

describe('developer face', () => {
  it('shows per-stage status, then the full report and an Accept button', async () => {
    vi.stubGlobal('fetch', vi.fn(async (u: any, init?: any) => {
      const url = String(u)
      if (url.endsWith('/report')) return { ok: true, json: async () => REPORT } as any
      if (url.endsWith('/accept')) return { ok: true, json: async () => ({ ...ENTRY, reviewed: true }) } as any
      return { ok: false, json: async () => ({}) } as any
    }))
    const onAccepted = vi.fn()
    render(<UploadDocument face="developer" onAccepted={onAccepted} />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    const xhr = FakeXHR.last!
    expect(screen.getAllByText('pending').length).toBe(8) // seven ingest stages plus enrich
    await act(async () => xhr.frame({ stage: 'extract', status: 'ok', elapsed_ms: 12, detail: 'pdf 2048 bytes' }))
    expect(screen.getByText(/12 ms · pdf 2048 bytes/)).toBeInTheDocument()
    await runToDone(xhr)
    expect(await screen.findByText(/pii_scan \(informational\)/)).toBeInTheDocument()
    expect(within(screen.getByLabelText('Personal-looking identifiers')).getByText(/hema… \[sec-13-p2\]/)).toBeInTheDocument()
    expect(within(screen.getByLabelText('Institutional contact details')).getByText(/1800… \[sec-13-p1\]/)).toBeInTheDocument()
    expect(screen.getByText(/180 clauses · body 141, heading 38, table_stub 1/)).toBeInTheDocument()
    expect(screen.getByText(/oversized check: ok/)).toBeInTheDocument()
    expect(within(screen.getByLabelText('Boilerplate')).getByText(/Page 1 of 9/)).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Accept' }))
    await act(async () => {})
    expect(onAccepted).toHaveBeenCalledWith(expect.objectContaining({ reviewed: true }))
    expect(screen.queryByRole('button', { name: 'Accept' })).toBeNull()
    expect(screen.getByText(/· reviewed/)).toBeInTheDocument()
  })

  it('an existing document comes back as a single done frame', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => REPORT }) as any))
    render(<UploadDocument face="developer" />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    await act(async () => {
      FakeXHR.last!.frame({ stage: 'done', status: 'ok', elapsed_ms: 0, existing: true, entry: ENTRY })
      FakeXHR.last!.finish()
    })
    expect(await screen.findByText(/doc_id abc123def4567890/)).toBeInTheDocument()
  })
})
