/**
 * One upload component for both routes.
 *
 * XHR rather than fetch, because fetch has no upload progress and a 20 MB
 * policy PDF on a slow link looks hung. The stage list renders immediately
 * with every stage pending, then fills from `ingest_progress` events over the
 * session's websocket; the clause preview follows on success.
 *
 * A refusal separates what the scanner found: institutional contact details
 * (a grievance mailbox, a regulator's address, a toll-free helpline -- allowed,
 * and kept in the fixture's review trail) from what looks like a person's own
 * data. Only the latter refuses. `allowOverride` decides whether a reason field
 * and a retry button are offered; the listener route never offers them.
 */
import { useRef, useState } from 'react'
import { Upload } from 'lucide-react'
import type { Session } from '../store/session'

export const STAGES = ['extract', 'structure', 'segment', 'normalize', 'pii scan', 'validate', 'write']

export type Hit = { label: string; redacted: string }

export type IngestResult = {
  ok: true
  name: string
  clause_count: number
  path: string
  report: string[]
  warnings: string[]
  institutional_hits?: Hit[]
  override_reason?: string | null
  preview: any[]
  note: string
}

export type IngestRefusal = {
  ok?: false
  stage?: string
  code?: number
  detail?: string
  error?: string
  report?: string[]
  personal_hits?: Hit[]
  institutional_hits?: Hit[]
  overridable?: boolean
  raw?: string
}

type Progress = { loaded: number; total: number } | null

export function fmtBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/** Parse what came back. Anything that is not JSON is surfaced verbatim. */
export function parseResponse(status: number, text: string): { ok: boolean; body: any } {
  try {
    const body = JSON.parse(text)
    return { ok: status >= 200 && status < 300, body }
  } catch {
    return { ok: false, body: { error: text, raw: text, stage: `http ${status}` } }
  }
}

function send(
  body: FormData | string,
  json: boolean,
  onProgress: (loaded: number, total: number) => void,
): Promise<{ status: number; text: string }> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', '/api/dev/ingest')
    if (json) xhr.setRequestHeader('content-type', 'application/json')
    xhr.upload.onprogress = (e) => {
      if (e.lengthComputable) onProgress(e.loaded, e.total)
    }
    xhr.onload = () => resolve({ status: xhr.status, text: xhr.responseText })
    xhr.onerror = () => reject(new Error('upload failed'))
    xhr.send(body)
  })
}

export default function UploadDocument({
  session,
  allowOverride,
  onSuccess,
  intro,
}: {
  session: Session
  allowOverride: boolean
  onSuccess?: (result: IngestResult) => void
  intro?: React.ReactNode
}) {
  const { state } = session
  const [over, setOver] = useState(false)
  const [url, setUrl] = useState('')
  const [file, setFile] = useState<File | null>(null)      // kept for retry-with-override
  const [progress, setProgress] = useState<Progress>(null)
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState<IngestResult | null>(null)
  const [refusal, setRefusal] = useState<IngestRefusal | null>(null)
  const [reason, setReason] = useState('')
  const [previewId, setPreviewId] = useState('')
  const fileInput = useRef<HTMLInputElement>(null)

  const submit = async (f: File | null, overrideReason?: string) => {
    setRefusal(null)
    setResult(null)
    session.dispatch({ type: 'clearIngest' })
    if (f && f.size > 20 * 1024 * 1024) {
      setRefusal({ stage: 'size', detail: 'File larger than 20 MB.' })
      return
    }
    setBusy(true)
    setProgress({ loaded: 0, total: f ? f.size : 0 })
    let body: FormData | string
    let json = false
    if (f) {
      const fd = new FormData()
      fd.append('file', f)
      if (overrideReason) fd.append('allow_pii_reason', overrideReason)
      body = fd
    } else {
      body = JSON.stringify(overrideReason ? { url, allow_pii_reason: overrideReason } : { url })
      json = true
    }
    let status = 0
    let text = ''
    try {
      ;({ status, text } = await send(body, json, (loaded, total) => setProgress({ loaded, total })))
    } catch (e) {
      setBusy(false)
      setRefusal({ stage: 'upload', detail: String((e as Error).message || e) })
      return
    }
    setBusy(false)
    const { ok, body: d } = parseResponse(status, text)
    if (ok) {
      setResult(d as IngestResult)
      setPreviewId(d.preview?.[0]?.id ?? '')
      onSuccess?.(d as IngestResult)
    } else {
      setRefusal(d as IngestRefusal)
    }
  }

  const pick = (f: File) => {
    setFile(f)
    submit(f)
  }

  const clause = result?.preview?.find((c: any) => c.id === previewId)
  const personal = refusal?.personal_hits || []
  const institutional = refusal?.institutional_hits || []
  const isPiiRefusal = refusal?.stage === 'pii scan'
  const canOverride = allowOverride && isPiiRefusal && refusal?.overridable !== false
  const pct = progress && progress.total > 0 ? Math.round((100 * progress.loaded) / progress.total) : 0

  return (
    <div className="upload">
      {intro}
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
          if (f) pick(f)
        }}
      >
        <Upload size={18} aria-hidden="true" />
        <div>Drop a PDF, DOCX, HTML, or text file</div>
        <input
          ref={fileInput}
          type="file"
          aria-label="Document file"
          disabled={busy}
          onChange={(e) => e.target.files?.[0] && pick(e.target.files[0])}
        />
      </div>
      <div className="toolbar" style={{ marginTop: 8 }}>
        <input
          type="url"
          placeholder="or a URL"
          aria-label="Document URL"
          value={url}
          disabled={busy}
          onChange={(e) => setUrl(e.target.value)}
        />
        <button
          onClick={() => {
            setFile(null)
            submit(null)
          }}
          disabled={busy || !url.trim()}
        >
          Ingest
        </button>
      </div>

      {progress && (
        <div className="progress" role="progressbar" aria-valuenow={pct} aria-valuemin={0} aria-valuemax={100}
             aria-label="Upload progress">
          <div className="bar"><div className="fill" style={{ width: `${pct}%` }} /></div>
          <div className="mono s-off">
            {progress.total > 0
              ? `${fmtBytes(progress.loaded)} of ${fmtBytes(progress.total)} · ${pct}%`
              : busy ? 'sending…' : 'sent'}
          </div>
        </div>
      )}

      {(progress || state.ingestStages.length > 0 || refusal) && (
        <div className="stages" style={{ marginTop: 10 }}>
          {STAGES.map((st) => {
            const hit = [...state.ingestStages].reverse().find((x) => x.stage === st)
            const done = result && !hit && st === 'write' ? { state: 'ok', detail: result.path } : hit
            return (
              <div className="stage" key={st}>
                <span className="st">{st}</span>
                <span className={`s-${done?.state ?? 'off'}`}>{done?.state ?? 'pending'}</span>
                <span className="s-off">{done?.detail ?? ''}</span>
              </div>
            )
          })}
        </div>
      )}

      {refusal && (
        <div className="refusal" style={{ marginTop: 8 }}>
          <div className="s-down mono">
            {refusal.stage ?? 'failed'}: {refusal.detail ?? refusal.error ?? 'failed'}
          </div>
          {institutional.length > 0 && (
            <div className="hits">
              <div className="hits-title s-ok">Institutional contact details — allowed</div>
              <ul aria-label="Institutional contact details">
                {institutional.map((h, i) => (
                  <li className="mono" key={`i${i}`}>{h.label}: {h.redacted}</li>
                ))}
              </ul>
            </div>
          )}
          {personal.length > 0 && (
            <div className="hits">
              <div className="hits-title s-down">Looks like personal data</div>
              <ul aria-label="Looks like personal data">
                {personal.map((h, i) => (
                  <li className="mono" key={`p${i}`}>{h.label}: {h.redacted}</li>
                ))}
              </ul>
            </div>
          )}
          {isPiiRefusal && !allowOverride && (
            <p className="notice">
              This document was not added because it looks like it contains someone&rsquo;s personal
              data. If that is a mistake, the team can review it with an override in the developer tools.
            </p>
          )}
          {isPiiRefusal && allowOverride && refusal.overridable === false && (
            <p className="notice s-down">
              A person&rsquo;s name beside an account number cannot be overridden.
            </p>
          )}
          {canOverride && (
            <div className="override">
              <input
                type="text"
                aria-label="Override reason"
                placeholder="Why this is not personal data (at least 12 characters)"
                value={reason}
                onChange={(e) => setReason(e.target.value)}
              />
              <button
                disabled={busy || reason.trim().length < 12 || (!file && !url.trim())}
                onClick={() => submit(file, reason.trim())}
              >
                Retry with override
              </button>
            </div>
          )}
        </div>
      )}

      {result && (
        <div className="result">
          <p className="notice mono">
            {result.clause_count} clauses to {result.path}
            {result.override_reason ? ` · override: ${result.override_reason}` : ''}
          </p>
          {(result.institutional_hits || []).length > 0 && (
            <p className="notice">
              Kept {result.institutional_hits!.length} institutional contact detail
              {result.institutional_hits!.length === 1 ? '' : 's'} in the review trail.
            </p>
          )}
          <p className="notice">{result.note}</p>
          {result.preview?.length > 0 && (
            <>
              <select aria-label="Clause preview" value={previewId} onChange={(e) => setPreviewId(e.target.value)}>
                {result.preview.map((c: any) => (
                  <option key={c.id} value={c.id}>{c.id}</option>
                ))}
              </select>
              {clause && (
                <div className="side-by-side">
                  <div className="box">{highlight(clause.text_display, clause.spoken_map, 0)}</div>
                  <div className="box spoken">{highlight(clause.text_spoken, clause.spoken_map, 1)}</div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

/** Lightly mark the spans the normalizer replaced, in both renderings. */
export function highlight(text: string, map: [number, number, string][] | undefined, side: 0 | 1) {
  if (!map) return text
  if (side === 1) return text
  const out: React.ReactNode[] = []
  let at = 0
  for (const [a, b, spoken] of map) {
    if (a < at) continue
    const display = text.slice(a, b)
    if (display !== spoken) {
      out.push(text.slice(at, a))
      out.push(<mark key={`${a}-${b}`}>{display}</mark>)
      at = b
    }
  }
  out.push(text.slice(at))
  return out
}
