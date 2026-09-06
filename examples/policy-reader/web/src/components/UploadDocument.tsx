/**
 * One upload, two faces.
 *
 * The listener face shows a progress bar keyed to the seven pipeline stages,
 * the elapsed time, and the document's title when it lands. Nothing else: no
 * stage names, no counts, no scan findings. The developer face shows the
 * stage list with per-stage status and time, then the ingest report in full
 * and an Accept button.
 *
 * One request does it: an XHR POST /documents gives upload progress in
 * bytes, and the response is a server-sent-event stream parsed out of
 * responseText as it arrives. The final event carries the library entry.
 */
import { useEffect, useRef, useState } from 'react'
import { Upload } from 'lucide-react'

export const STAGES = ['extract', 'structure', 'segment', 'normalize', 'pii_scan', 'validate', 'write']

export type LibraryEntry = {
  doc_id: string
  name: string
  title: string
  reviewed: boolean
  readable: boolean
  clause_count: number
  report?: string | null
}

export type StageEvent = { stage: string; status: string; elapsed_ms: number; detail?: string; entry?: LibraryEntry; error?: string; existing?: boolean }

export type Report = {
  doc_id: string
  name: string
  title: string
  elapsed_ms: Record<string, number>
  pii_scan: { personal: Hit[]; institutional: Hit[]; note: string }
  validate: {
    clause_count: number
    by_kind: Record<string, number>
    body_clauses: number
    readable: boolean
    splits: string[]
    folds_and_merges: string[]
    oversized_ok: boolean
    max_clause_chars: number
    boilerplate_first_15: { id: string; text: string; reason: string }[]
    expected_range: { reference: string; words: number; scaled_range: number[]; in_range: boolean }
    other_notes: string[]
    warnings: string[]
  }
}
export type Hit = { label: string; redacted: string; clause_id: string | null }

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** Parse complete `data: {...}` frames out of an SSE text buffer. */
export function parseSse(text: string, from: number): { events: StageEvent[]; consumed: number } {
  const events: StageEvent[] = []
  let at = from
  for (;;) {
    const end = text.indexOf('\n\n', at)
    if (end < 0) break
    const frame = text.slice(at, end)
    at = end + 2
    for (const line of frame.split('\n')) {
      if (line.startsWith('data:')) {
        try {
          events.push(JSON.parse(line.slice(5).trim()))
        } catch {
          /* a partial or non-JSON line: ignore */
        }
      }
    }
  }
  return { events, consumed: at }
}

type Progress = { uploadLoaded: number; uploadTotal: number; stagesDone: string[]; stageStatus: Record<string, StageEvent> }

function post(
  body: FormData | string,
  json: boolean,
  onUpload: (loaded: number, total: number) => void,
  onEvent: (ev: StageEvent) => void,
): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/documents')
    if (json) xhr.setRequestHeader('content-type', 'application/json')
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onUpload(e.loaded, e.total)
    }
    let consumed = 0
    const drain = () => {
      const { events, consumed: next } = parseSse(xhr.responseText || '', consumed)
      consumed = next
      for (const ev of events) onEvent(ev)
    }
    xhr.onprogress = drain
    xhr.onload = () => {
      drain()
      resolve({ status: xhr.status, text: xhr.responseText })
    }
    xhr.onerror = () => reject(new Error('upload failed'))
    xhr.send(body)
  })
}

export default function UploadDocument({
  face,
  onDone,
  onAccepted,
}: {
  face: 'listener' | 'developer'
  onDone?: (entry: LibraryEntry) => void
  onAccepted?: (entry: LibraryEntry) => void
}) {
  const [over, setOver] = useState(false)
  const [url, setUrl] = useState('')
  const [busy, setBusy] = useState(false)
  const [progress, setProgress] = useState<Progress | null>(null)
  const [entry, setEntry] = useState<LibraryEntry | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [report, setReport] = useState<Report | null>(null)
  const [elapsed, setElapsed] = useState(0)
  const started = useRef<number | null>(null)

  useEffect(() => {
    if (!busy) return
    const id = window.setInterval(() => {
      if (started.current !== null) setElapsed(Math.round((Date.now() - started.current) / 1000))
    }, 250)
    return () => window.clearInterval(id)
  }, [busy])

  const submit = async (f: File | null) => {
    setError(null)
    setEntry(null)
    setReport(null)
    if (f && f.size > 25 * 1024 * 1024) {
      setError('File larger than 25 MB.')
      return
    }
    setBusy(true)
    started.current = Date.now()
    setElapsed(0)
    setProgress({ uploadLoaded: 0, uploadTotal: f ? f.size : 0, stagesDone: [], stageStatus: {} })
    let body: FormData | string
    let json = false
    if (f) {
      const fd = new FormData()
      fd.append('file', f)
      body = fd
    } else {
      body = JSON.stringify({ url })
      json = true
    }
    let final: StageEvent | null = null
    let status = 0
    let text = ''
    try {
      ;({ status, text } = await post(
        body,
        json,
        (loaded, total) => setProgress((p) => (p ? { ...p, uploadLoaded: loaded, uploadTotal: total } : p)),
        (ev) => {
          if (ev.stage === 'done') {
            final = ev
            return
          }
          setProgress((p) =>
            p
              ? { ...p, stagesDone: p.stagesDone.includes(ev.stage) ? p.stagesDone : [...p.stagesDone, ev.stage],
                  stageStatus: { ...p.stageStatus, [ev.stage]: ev } }
              : p,
          )
        },
      ))
    } catch (e) {
      setBusy(false)
      setError(String((e as Error).message || e))
      return
    }
    setBusy(false)
    if (status < 200 || status >= 300) {
      // The two HTTP errors: too large, or an unsupported type.
      try {
        setError(JSON.parse(text).error || text)
      } catch {
        setError(text || `HTTP ${status}`)
      }
      return
    }
    const f2 = final as StageEvent | null
    if (!f2 || f2.status !== 'ok' || !f2.entry) {
      setError(f2?.error || 'The upload did not complete.')
      return
    }
    setEntry(f2.entry)
    onDone?.(f2.entry)
    if (face === 'developer') {
      try {
        const r = await fetch(`/documents/${encodeURIComponent(f2.entry.doc_id)}/report`)
        if (r.ok) setReport(await r.json())
      } catch {
        /* the report panel stays empty */
      }
    }
  }

  const accept = async () => {
    if (!entry) return
    const r = await fetch(`/documents/${encodeURIComponent(entry.doc_id)}/accept`, { method: 'POST' })
    if (r.ok) {
      const e = (await r.json()) as LibraryEntry
      setEntry(e)
      onAccepted?.(e)
    }
  }

  // Progress fraction: the upload is the first slice, then one slice per stage.
  const frac = (() => {
    if (!progress) return 0
    const up = progress.uploadTotal > 0 ? progress.uploadLoaded / progress.uploadTotal : 1
    const stageFrac = progress.stagesDone.length / STAGES.length
    return entry ? 1 : 0.15 * up + 0.85 * stageFrac
  })()
  const pct = Math.round(frac * 100)

  return (
    <div className="upload">
      <div
        className={`drop${over ? ' over' : ''}`}
        onDragOver={(e) => {
          e.preventDefault()
          setOver(true)
        }}
        onDragLeave={() => setOver(false)}
        onDrop={(e) => {
          e.preventDefault()
          setOver(false)
          const f = e.dataTransfer.files?.[0]
          if (f) submit(f)
        }}
      >
        <Upload size={18} aria-hidden="true" />
        <div>Drop a PDF here, or choose a file</div>
        <input type="file" aria-label="Document file" disabled={busy}
               onChange={(e) => e.target.files?.[0] && submit(e.target.files[0])} />
      </div>
      {face === 'developer' && (
        <div className="toolbar" style={{ marginTop: 8 }}>
          <input type="url" placeholder="or a URL" aria-label="Document URL" value={url} disabled={busy}
                 onChange={(e) => setUrl(e.target.value)} />
          <button onClick={() => submit(null)} disabled={busy || !url.trim()}>Ingest</button>
        </div>
      )}

      {progress && (
        <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
             aria-label="Upload progress">
          <div className="bar"><div className="fill" style={{ width: `${pct}%` }} /></div>
          <div className="mono s-off">
            {entry ? `done in ${elapsed} s` : busy ? `${pct}% · ${elapsed} s` : ''}
          </div>
        </div>
      )}

      {face === 'developer' && progress && (
        <div className="stages" style={{ marginTop: 10 }}>
          {STAGES.map((st) => {
            const ev = progress.stageStatus[st]
            return (
              <div className="stage" key={st}>
                <span className="st">{st}</span>
                <span className={`s-${ev ? (ev.status === 'ok' ? 'ok' : 'warn') : 'off'}`}>{ev ? ev.status : 'pending'}</span>
                <span className="s-off">{ev ? `${Math.round(ev.elapsed_ms)} ms${ev.detail ? ` · ${ev.detail}` : ''}` : ''}</span>
              </div>
            )
          })}
        </div>
      )}

      {error && <p className="notice s-down">{error}</p>}

      {entry && face === 'listener' && (
        <p className="notice">
          Added: <strong>{entry.title}</strong>.
          {!entry.readable && ' No readable text found.'}
        </p>
      )}

      {entry && face === 'developer' && (
        <div className="result">
          <p className="notice mono">
            {entry.title} · doc_id {entry.doc_id} · {entry.clause_count} clauses ·{' '}
            {entry.readable ? 'readable' : 'no readable text'} · {entry.reviewed ? 'reviewed' : 'unreviewed'}
          </p>
          {!entry.reviewed && (
            <button onClick={accept} aria-label="Accept">Accept</button>
          )}
          {report && <ReportView report={report} />}
        </div>
      )}
    </div>
  )
}

/** The ingest report, in full. Informational: nothing here blocked anything. */
export function ReportView({ report }: { report: Report }) {
  const v = report.validate
  const p = report.pii_scan
  return (
    <div className="report">
      <h3>pii_scan (informational)</h3>
      <p className="notice">{p.note}</p>
      {p.institutional.length > 0 && (
        <>
          <div className="hits-title s-ok">Institutional contact details</div>
          <ul aria-label="Institutional contact details">
            {p.institutional.map((h, i) => (
              <li className="mono" key={`i${i}`}>{h.label}: {h.redacted}{h.clause_id ? ` [${h.clause_id}]` : ''}</li>
            ))}
          </ul>
        </>
      )}
      {p.personal.length > 0 && (
        <>
          <div className="hits-title s-warn">Personal-looking identifiers</div>
          <ul aria-label="Personal-looking identifiers">
            {p.personal.map((h, i) => (
              <li className="mono" key={`p${i}`}>{h.label}: {h.redacted}{h.clause_id ? ` [${h.clause_id}]` : ''}</li>
            ))}
          </ul>
        </>
      )}
      {p.institutional.length === 0 && p.personal.length === 0 && <p className="notice">Nothing found.</p>}

      <h3>validate (informational)</h3>
      <div className="mono">
        {v.clause_count} clauses · {Object.entries(v.by_kind).map(([k, n]) => `${k} ${n}`).join(', ')}
      </div>
      <div className="mono">
        {v.expected_range.reference}; this document {v.expected_range.words} words →{' '}
        {v.expected_range.scaled_range[0]}–{v.expected_range.scaled_range[1]}; got {v.clause_count}{' '}
        ({v.expected_range.in_range ? 'in range' : 'outside range'})
      </div>
      <div className="mono">
        oversized check: {v.oversized_ok ? 'ok' : 'FAILED'} (longest {v.max_clause_chars} chars) · splits {v.splits.length}
        {v.folds_and_merges.length > 0 ? ` · ${v.folds_and_merges.join('; ')}` : ''}
      </div>
      {v.splits.length > 0 && (
        <ul aria-label="Splits">{v.splits.map((sp, i) => <li className="mono" key={i}>{sp}</li>)}</ul>
      )}
      <div className="hits-title">Boilerplate (first {v.boilerplate_first_15.length})</div>
      {v.boilerplate_first_15.length === 0 ? (
        <p className="notice">None demoted.</p>
      ) : (
        <ul aria-label="Boilerplate">
          {v.boilerplate_first_15.map((b) => (
            <li className="mono" key={b.id}>[{b.id}] ({b.reason}) {b.text}</li>
          ))}
        </ul>
      )}
      {v.warnings.length > 0 && (
        <ul aria-label="Notes">{v.warnings.map((w, i) => <li className="mono" key={i}>{w}</li>)}</ul>
      )}
    </div>
  )
}
