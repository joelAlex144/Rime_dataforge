import { act, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import UploadDocument from './UploadDocument'
import { initialState } from '../store/reducer'
import type { Session } from '../store/session'

/** Enough of XMLHttpRequest to drive upload progress and the response. */
class FakeXHR {
  static last: FakeXHR | null = null
  static all: FakeXHR[] = []
  upload: { onprogress: ((e: any) => void) | null } = { onprogress: null }
  onload: (() => void) | null = null
  onerror: (() => void) | null = null
  status = 0
  responseText = ''
  headers: Record<string, string> = {}
  body: any = null
  open() {}
  setRequestHeader(k: string, v: string) {
    this.headers[k] = v
  }
  send(body: any) {
    this.body = body
    FakeXHR.last = this
    FakeXHR.all.push(this)
  }
  progress(loaded: number, total: number) {
    this.upload.onprogress?.({ lengthComputable: true, loaded, total })
  }
  respond(status: number, text: string) {
    this.status = status
    this.responseText = text
    this.onload?.()
  }
}

function session(over: Partial<typeof initialState> = {}): Session {
  return {
    state: { ...initialState, ...over },
    dispatch: vi.fn(),
    send: vi.fn(),
    interrupt: vi.fn(),
    play: vi.fn(),
    pause: vi.fn(),
    ask: vi.fn(),
    resume: vi.fn(),
    open: vi.fn(),
    jump: vi.fn(),
    player: {} as any,
  }
}

const file = () => new File([new Uint8Array(1024)], 'policy.txt', { type: 'text/plain' })

beforeEach(() => {
  FakeXHR.last = null
  FakeXHR.all = []
  vi.stubGlobal('XMLHttpRequest', FakeXHR as any)
})
afterEach(() => vi.unstubAllGlobals())

const REFUSAL = {
  ok: false, stage: 'pii scan', code: 2, overridable: true,
  detail: "Refused: looks like someone's personal data.",
  personal_hits: [{ label: 'email', redacted: 'rame…' }, { label: 'phone', redacted: '9876…' }],
  institutional_hits: [{ label: 'email', redacted: 'grie…' }, { label: 'phone', redacted: '1800…' }],
}

describe('UploadDocument', () => {
  it('shows a real progress bar with bytes and percent, and the stage list pending at once', async () => {
    render(<UploadDocument session={session()} allowOverride />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    expect(FakeXHR.last).not.toBeNull()
    const bar = screen.getByRole('progressbar')
    expect(bar).toBeInTheDocument()
    // Every stage is visible and pending before any server event arrives.
    const pending = screen.getAllByText('pending')
    expect(pending.length).toBe(7)
    await act(async () => FakeXHR.last!.progress(512, 1024))
    expect(screen.getByText(/512 B of 1 KB · 50%/)).toBeInTheDocument()
    expect(bar).toHaveAttribute('aria-valuenow', '50')
  })

  it('surfaces a non-JSON error verbatim', async () => {
    render(<UploadDocument session={session()} allowOverride />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    await act(async () => FakeXHR.last!.respond(502, '<html>bad gateway from the proxy</html>'))
    expect(screen.getByText(/<html>bad gateway from the proxy<\/html>/)).toBeInTheDocument()
    expect(screen.getByText(/http 502/)).toBeInTheDocument()
  })

  it('renders a PII refusal as two labelled lists', async () => {
    render(<UploadDocument session={session()} allowOverride />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    await act(async () => FakeXHR.last!.respond(422, JSON.stringify(REFUSAL)))
    const inst = screen.getByLabelText('Institutional contact details')
    const pers = screen.getByLabelText('Looks like personal data')
    expect(within(inst).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'email: grie…', 'phone: 1800…',
    ])
    expect(within(pers).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'email: rame…', 'phone: 9876…',
    ])
    expect(screen.getByText(/Institutional contact details — allowed/)).toBeInTheDocument()
  })

  it('retry with override resubmits the SAME file with the reason', async () => {
    render(<UploadDocument session={session()} allowOverride />)
    const f = file()
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [f] } })
    const first = FakeXHR.last!
    expect((first.body as FormData).get('file')).toBe(f)
    await act(async () => first.respond(422, JSON.stringify(REFUSAL)))
    const retry = screen.getByText('Retry with override') as HTMLButtonElement
    expect(retry.disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('Override reason'), { target: { value: 'sample person is fictional' } })
    expect(retry.disabled).toBe(false)
    fireEvent.click(retry)
    const second = FakeXHR.last!
    expect(second).not.toBe(first)
    const fd = second.body as FormData
    expect(fd.get('file')).toBe(f)
    expect(fd.get('allow_pii_reason')).toBe('sample person is fictional')
  })

  it('without override, a refusal explains and points at the developer tools', async () => {
    render(<UploadDocument session={session()} allowOverride={false} />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    await act(async () => FakeXHR.last!.respond(422, JSON.stringify(REFUSAL)))
    expect(screen.queryByText('Retry with override')).toBeNull()
    expect(screen.queryByLabelText('Override reason')).toBeNull()
    expect(screen.getByText(/developer tools/)).toBeInTheDocument()
  })

  it('a name beside an account number is never overridable', async () => {
    render(<UploadDocument session={session()} allowOverride />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    await act(async () =>
      FakeXHR.last!.respond(422, JSON.stringify({ ...REFUSAL, overridable: false,
        personal_hits: [{ label: 'name_with_account_number', redacted: 'Jane…' }] })))
    expect(screen.queryByText('Retry with override')).toBeNull()
    expect(screen.getByText(/cannot be overridden/)).toBeInTheDocument()
  })

  it('success shows the override reason in the result', async () => {
    const onSuccess = vi.fn()
    render(<UploadDocument session={session()} allowOverride onSuccess={onSuccess} />)
    fireEvent.change(screen.getByLabelText('Document file'), { target: { files: [file()] } })
    await act(async () =>
      FakeXHR.last!.respond(200, JSON.stringify({
        ok: true, name: 'policy', clause_count: 25, path: 'fixtures/unreviewed/policy.json',
        report: [], warnings: [], preview: [], override_reason: 'sample person is fictional',
        institutional_hits: [{ label: 'email', redacted: 'grie…' }],
        note: 'Written to fixtures/unreviewed/.',
      })))
    expect(screen.getByText(/override: sample person is fictional/)).toBeInTheDocument()
    expect(screen.getByText(/Kept 1 institutional contact detail in the review trail/)).toBeInTheDocument()
    expect(onSuccess).toHaveBeenCalledWith(expect.objectContaining({ name: 'policy' }))
  })
})
