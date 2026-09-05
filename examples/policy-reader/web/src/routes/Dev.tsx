/**
 * Diagnostics route.
 *
 * Same store as the listener, different rendering: here the identifiers are
 * the point. Everything shown is either an event record or a number computed
 * from this session's event log, so nothing on this page can disagree with
 * what the listener heard. "far end" stays n/a until the acceptance harness
 * posts a real measurement; it is never filled in with a server-side proxy.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { Download, Play, Upload } from 'lucide-react'
import { useSession } from '../store/session'
import type { Cell, EventRecord } from '../store/reducer'

const STAGES = ['extract', 'structure', 'segment', 'normalize', 'pii scan', 'validate']

function cls(c?: Cell) {
  return `v s-${c?.state ?? 'off'}`
}

export default function Dev() {
  const s = useSession()
  const { state } = s
  const [filter, setFilter] = useState('')
  const [follow, setFollow] = useState(true)
  const [traces, setTraces] = useState<{ name: string; bytes: number }[]>([])
  const [trace, setTrace] = useState('')
  const [ingest, setIngest] = useState<any>(null)
  const [ingestErr, setIngestErr] = useState<any>(null)
  const [previewId, setPreviewId] = useState('')
  const [over, setOver] = useState(false)
  const [url, setUrl] = useState('')
  const listRef = useRef<HTMLDivElement>(null)

  const dev = !!state.status.dev

  useEffect(() => {
    fetch('/api/traces')
      .then((r) => r.json())
      .then((d) => setTraces(d.traces || []))
      .catch(() => setTraces([]))
  }, [state.replaying])

  const events = useMemo(() => {
    const f = filter.trim().toLowerCase()
    const rows = f
      ? state.events.filter((e) =>
          `${e.type ?? ''} ${e.context_id ?? ''} ${e.unit_id ?? ''} ${e.document ?? ''}`
            .toLowerCase()
            .includes(f),
        )
      : state.events
    return rows.slice(-500)
  }, [state.events, filter])

  useEffect(() => {
    if (follow && listRef.current) listRef.current.scrollTop = listRef.current.scrollHeight
  }, [events.length, follow])

  // /api/status carries the provider descriptor too, so the badge is populated
  // from the poll even before a websocket hello has arrived.
  const provider: Record<string, any> =
    (Object.keys(state.provider || {}).length ? state.provider : (state.status.provider as any)) || {}
  const isFake = (provider.provider ?? '') === 'fake'

  const boundary = useMemo(() => {
    const r = [...state.events].reverse().find((e) => e.type === 'unit_truncated')
    if (!r) return 'n/a'
    return `${r.context_id ?? r.unit_id} · ${r.rendered_ms ?? '?'} ms -> char ${r.char_end} of ${r.of ?? '?'}`
  }, [state.events])

  const resumeRow = useMemo(() => {
    const r = [...state.events].reverse().find((e) => e.type === 'position_restored')
    if (!r) return 'n/a'
    return `sentence ${r.sentence_index ?? '?'} · char_start ${r.char_start ?? '?'}`
  }, [state.events])

  const audibleStop = useMemo(() => {
    const p50 = state.metrics?.flush_ack_p50
    return `${p50 == null ? 'n/a' : `flush ack ${p50} ms`} · far end n/a`
  }, [state.metrics])

  const submitIngest = async (file?: File) => {
    setIngestErr(null)
    setIngest(null)
    s.dispatch({ type: 'clearIngest' })
    let res: Response
    if (file) {
      if (file.size > 20 * 1024 * 1024) {
        setIngestErr({ detail: 'File larger than 20 MB.' })
        return
      }
      const fd = new FormData()
      fd.append('file', file)
      res = await fetch('/api/dev/ingest', { method: 'POST', body: fd })
    } else {
      res = await fetch('/api/dev/ingest', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ url }),
      })
    }
    const d = await res.json()
    if (res.ok) {
      setIngest(d)
      setPreviewId(d.preview?.[0]?.id ?? '')
    } else setIngestErr(d)
  }

  const clause = ingest?.preview?.find((c: any) => c.id === previewId)

  return (
    <div className="dev">
      <header className="dev-head">
        <h1>Diagnostics</h1>
        <span className="mono">{state.sessionId ?? 'no session'}</span>
        <span className={`badge ${isFake ? 'fake' : 'rime'}`}>
          {['provider', 'modelId', 'speaker', 'lang', 'audioFormat', 'samplingRate']
            .map((k) => `${k}=${provider[k] ?? '?'}`)
            .join(' ')}
        </span>
        <a href={`/api/events?after=0`} download={`${state.sessionId}.jsonl`}>
          <button>
            <Download size={16} aria-hidden="true" /> Export trace
          </button>
        </a>
        {dev && (
          <select
            aria-label="Provider"
            value={isFake ? 'fake' : 'rime'}
            onChange={(e) =>
              fetch('/api/dev/provider', {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ name: e.target.value }),
              })
            }
          >
            <option value="rime">rime</option>
            <option value="fake">fake</option>
          </select>
        )}
        {state.replaying && <span className="badge">Replaying {state.replaying}</span>}
        <Link to="/">Listener</Link>
      </header>

      <div className="strip">
        {(
          [
            ['ingest', state.status.ingest],
            ['normalize', state.status.normalize],
            ['rime ws3', state.status.rime_ws],
            ['client ws', state.status.client_ws],
            ['stt', state.status.stt],
            ['llm', state.status.llm],
          ] as [string, Cell | undefined][]
        ).map(([k, c]) => (
          <div className="cell" key={k}>
            <div className="k">{k}</div>
            <div className={cls(c)}>
              {c?.state ?? 'off'} · {c?.detail ?? ''}
            </div>
          </div>
        ))}
      </div>

      <div className="cols">
        <section className="panel">
          <h2>Ingestion</h2>
          {!dev ? (
            <p className="notice">
              Upload is disabled outside dev mode. Add fixtures with scripts/ingest.py.
            </p>
          ) : (
            <>
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
                  if (f) submitIngest(f)
                }}
              >
                <Upload size={18} aria-hidden="true" />
                <div>Drop a PDF, DOCX, HTML, or text file</div>
                <input
                  type="file"
                  aria-label="Document file"
                  onChange={(e) => e.target.files?.[0] && submitIngest(e.target.files[0])}
                />
              </div>
              <div className="toolbar" style={{ marginTop: 8 }}>
                <input
                  type="url"
                  placeholder="or a URL"
                  aria-label="Document URL"
                  value={url}
                  onChange={(e) => setUrl(e.target.value)}
                />
                <button onClick={() => submitIngest()} disabled={!url.trim()}>
                  Ingest
                </button>
              </div>
            </>
          )}

          {(state.ingestStages.length > 0 || ingestErr) && (
            <div style={{ marginTop: 10 }}>
              {STAGES.map((st) => {
                const hit = [...state.ingestStages].reverse().find((x) => x.stage === st)
                return (
                  <div className="stage" key={st}>
                    <span className="st">{st}</span>
                    <span className={`s-${hit?.state ?? 'off'}`}>{hit?.state ?? 'pending'}</span>
                    <span className="s-off">{hit?.detail ?? ''}</span>
                  </div>
                )
              })}
            </div>
          )}
          {ingestErr && (
            <div style={{ marginTop: 8 }}>
              <div className="s-down mono">
                {ingestErr.stage ?? 'failed'}: {ingestErr.detail ?? ingestErr.error ?? 'failed'}
              </div>
              {(ingestErr.redacted_hits || []).map((h: string, i: number) => (
                <div className="mono s-warn" key={i}>
                  {h}
                </div>
              ))}
            </div>
          )}

          {ingest && (
            <>
              <p className="notice mono">
                {ingest.clause_count} clauses to {ingest.path}
              </p>
              <p className="notice">{ingest.note}</p>
              <select
                aria-label="Clause preview"
                value={previewId}
                onChange={(e) => setPreviewId(e.target.value)}
              >
                {(ingest.preview || []).map((c: any) => (
                  <option key={c.id} value={c.id}>
                    {c.id}
                  </option>
                ))}
              </select>
              {clause && (
                <div className="side-by-side">
                  <div className="box">{highlight(clause.text_display, clause.spoken_map, 0)}</div>
                  <div className="box spoken">{highlight(clause.text_spoken, clause.spoken_map, 1)}</div>
                </div>
              )}
              <div className="toolbar" style={{ marginTop: 8 }}>
                <button
                  onClick={() =>
                    fetch('/api/dev/open?unreviewed=1', {
                      method: 'POST',
                      headers: { 'content-type': 'application/json' },
                      body: JSON.stringify({ name: ingest.name }),
                    })
                  }
                >
                  Open in listener (unreviewed)
                </button>
                <button onClick={() => setIngest(null)}>Discard</button>
              </div>
            </>
          )}
        </section>

        <section className="panel">
          <h2>Rime and playback</h2>
          <div className="metrics">
            <Metric k="ttfb p50" v={fmt(state.metrics?.ttfb_p50, 'ms')} />
            <Metric k="ttfb p95" v={fmt(state.metrics?.ttfb_p95, 'ms')} />
            <Metric
              k="fenced after clear"
              v={state.metrics?.fenced_bytes_after_clear == null ? 'n/a' : `${state.metrics.fenced_bytes_after_clear} B`}
            />
          </div>
          <div className="metrics">
            <Metric
              k="interpolated spans"
              v={
                state.metrics?.spans_total == null
                  ? 'n/a'
                  : `${state.metrics.interpolated_spans} / ${state.metrics.spans_total}`
              }
            />
            <Metric k="flush ack p50" v={fmt(state.metrics?.flush_ack_p50, 'ms')} />
            <Metric k="contexts" v={String(state.contexts.length)} />
          </div>

          <table>
            <thead>
              <tr>
                <th>contextId</th>
                <th>turn</th>
                <th>state</th>
                <th>bytes</th>
                <th>rendered ms</th>
              </tr>
            </thead>
            <tbody>
              {state.contexts.map((c) => (
                <tr key={c.context_id} className={c.state}>
                  <td>{c.context_id}</td>
                  <td>{c.turn_id}</td>
                  <td>{c.state}</td>
                  <td>{c.bytes}</td>
                  <td>{c.rendered_ms}</td>
                </tr>
              ))}
              {state.contexts.length === 0 && (
                <tr>
                  <td colSpan={5} className="s-off">
                    no contexts yet
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          <div className="kv" style={{ marginTop: 10 }}>
            <div className="k">boundary</div>
            <div className="v">{boundary}</div>
            <div className="k">resume</div>
            <div className="v">{resumeRow}</div>
            <div className="k">audible stop</div>
            <div className="v">{audibleStop}</div>
          </div>
        </section>
      </div>

      <section className="events">
        <div className="toolbar">
          <input
            type="text"
            placeholder="filter by type or unit"
            aria-label="Filter events"
            value={filter}
            onChange={(e) => setFilter(e.target.value)}
          />
          <span className="mono">{events.length} records</span>
          <label className="mono">
            <input type="checkbox" checked={follow} onChange={(e) => setFollow(e.target.checked)} />{' '}
            follow
          </label>
          <select aria-label="Trace" value={trace} onChange={(e) => setTrace(e.target.value)}>
            <option value="">select a trace</option>
            {traces.map((t) => (
              <option key={t.name} value={t.name}>
                {t.name} ({t.bytes} B)
              </option>
            ))}
          </select>
          <button
            disabled={!trace || !!state.replaying}
            onClick={() =>
              fetch('/api/replay', {
                method: 'POST',
                headers: { 'content-type': 'application/json' },
                body: JSON.stringify({ trace }),
              })
            }
          >
            <Play size={16} aria-hidden="true" /> Replay trace
          </button>
        </div>
        <div className="event-list" ref={listRef} data-testid="event-list">
          {events.map((e, i) => (
            <EventRow key={i} rec={e} />
          ))}
        </div>
      </section>
    </div>
  )
}

function Metric({ k, v }: { k: string; v: string }) {
  return (
    <div className="metric">
      <div className="k">{k}</div>
      <div className="v">{v}</div>
    </div>
  )
}

function fmt(v: number | null | undefined, unit: string) {
  return v == null ? 'n/a' : `${v} ${unit}`
}

function EventRow({ rec }: { rec: EventRecord }) {
  const skip = new Set(['ts_ms', 'wall', 'session_id', 'type', 'seq', 'context_id', 'unit_id'])
  const kv = Object.entries(rec)
    .filter(([k]) => !skip.has(k))
    .map(([k, v]) => `${k}=${typeof v === 'object' ? JSON.stringify(v) : v}`)
    .join(' ')
  return (
    <div className={`event-row${rec.type === 'result_fenced' ? ' muted' : ''}`}>
      <span>{typeof rec.ts_ms === 'number' ? (rec.ts_ms / 1000).toFixed(2) : '-'}</span>
      <span className="ty">{String(rec.type ?? '')}</span>
      <span className="cx">{String(rec.context_id ?? rec.unit_id ?? '')}</span>
      <span>{kv}</span>
    </div>
  )
}

/** Lightly mark the spans the normalizer replaced, in both renderings. */
function highlight(text: string, map: [number, number, string][] | undefined, side: 0 | 1) {
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
