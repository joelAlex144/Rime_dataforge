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
import { Download, Hand, Pause, Play } from 'lucide-react'
import { useSession } from '../store/session'
import type { Cell, EventRecord } from '../store/reducer'
import UploadDocument from '../components/UploadDocument'

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
  const [q, setQ] = useState('')
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

      <div className="toolbar transport-dev" aria-label="Transport">
        {(() => {
          const sounding = state.phase === 'playing' || state.phase === 'speaking'
          const busy = !!state.replaying
          return (
            <>
              <button
                className="primary"
                onClick={() => (sounding ? s.pause() : s.play())}
                disabled={busy}
                aria-label={sounding ? 'Pause' : 'Play'}
              >
                {sounding ? <Pause size={16} aria-hidden="true" /> : <Play size={16} aria-hidden="true" />}
                {sounding ? 'Pause' : 'Play'}
              </button>
              <button onClick={() => s.interrupt()} disabled={busy} aria-label="Stop and ask">
                <Hand size={16} aria-hidden="true" /> Stop and ask
              </button>
              <form
                onSubmit={(e) => {
                  e.preventDefault()
                  if (!q.trim()) return
                  if (sounding) s.interrupt()
                  s.ask(q.trim())
                  setQ('')
                }}
                style={{ display: 'contents' }}
              >
                <input
                  type="text"
                  value={q}
                  onChange={(e) => setQ(e.target.value)}
                  placeholder="Ask about what was heard"
                  aria-label="Ask"
                />
              </form>
              <span className={`badge ${state.audioSink ? 'rime' : ''}`} aria-label="Audio sink">
                {state.audioSink
                  ? 'voice: this tab'
                  : state.anySink
                    ? 'voice: another tab'
                    : 'voice: none (press play)'}
              </span>
              <span className="mono s-off">{state.phase}</span>
              <span className="mono" aria-label="Current document">
                document: {state.current ?? 'none'}
              </span>
            </>
          )
        })()}
      </div>

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
          <UploadDocument face="developer" />
          <div className="ingested" style={{ marginTop: 10 }}>
            <div className="s-off mono">Library (index.json)</div>
            {state.documents.map((d) => (
              <div className="stage" key={d.name}>
                <span className="st mono">{d.name}{state.current === d.name ? ' (open)' : ''}</span>
                <span className="s-off">{d.section_count} sections</span>
                <span className={d.reviewed ? 's-ok' : 's-warn'}>{d.reviewed ? 'reviewed' : 'unreviewed'}</span>
                {d.readable === false && <span className="s-warn">no readable text</span>}
                {!d.reviewed && d.doc_id && (
                  <button
                    aria-label={`Accept ${d.name}`}
                    onClick={() => fetch(`/documents/${encodeURIComponent(d.doc_id!)}/accept`, { method: 'POST' })}
                  >
                    Accept
                  </button>
                )}
                <button onClick={() => s.open(d.name)}>Open</button>
              </div>
            ))}
          </div>
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
