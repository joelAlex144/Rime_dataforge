/**
 * Listener route.
 *
 * Audience: someone having a document read to them. Every identifier is hidden
 * -- no clause ids, no milliseconds, no provider name, no turn ids. What the
 * reader shows about position comes from acks, so text after the delivery
 * boundary stays grey even though the server has already sent that audio.
 */
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import {
  ChevronLeft,
  ChevronRight,
  CornerDownRight,
  FileText,
  FilePlus,
  Hand,
  MessageSquare,
  Mic,
  Pause,
  Play,
  Volume2,
  X,
} from 'lucide-react'
import { useSession } from '../store/session'
import type { DocEntry } from '../store/reducer'
import UploadDocument from '../components/UploadDocument'
import { useVoiceInput } from '../hooks/useVoiceInput'

const ICON = 18

function railSubtitle(d: DocEntry): string {
  if (d.readable === false) return 'No readable text found'
  if (d.navigator === 'preparing' && !d.progress.started) return 'Preparing overview…'
  if (d.progress.finished) return 'Finished'
  if (!d.progress.started) return 'Not started'
  const sec = Math.max(1, d.progress.current_section_index)
  return `Section ${sec} of ${d.section_count} · ${d.progress.minutes_left} min left`
}

export default function Listener() {
  const s = useSession()
  const { state } = s
  const inputRef = useRef<HTMLInputElement>(null)
  const dialogRef = useRef<HTMLDialogElement>(null)
  const [q, setQ] = useState('')
  const [r, setR] = useState('')
  // On by default: the script is the primary "what's actually happening" view
  // (screen 3 of the screen map), so it's the reader's normal state rather
  // than something to go dig for -- the footer button just hides it.
  const [showScript, setShowScript] = useState(true)
  const scriptRef = useRef<HTMLDivElement>(null)
  const scriptSentinelRef = useRef<HTMLDivElement>(null)
  // Renders the script list a batch at a time as the panel is scrolled,
  // instead of mounting every clause's <p> the moment it opens -- opening a
  // 40-clause document shouldn't mean rendering 40 paragraphs no one has
  // scrolled to yet.
  const SCRIPT_BATCH = 12
  const [scriptVisible, setScriptVisible] = useState(SCRIPT_BATCH)
  const [railOpen, setRailOpen] = useState(() => {
    try {
      return localStorage.getItem('rail_open') !== '0'
    } catch {
      return true
    }
  })
  const toggleRail = () => {
    setRailOpen((v) => {
      const next = !v
      try {
        localStorage.setItem('rail_open', next ? '1' : '0')
      } catch {
        /* private browsing etc. -- just don't persist */
      }
      return next
    })
  }
  const voice = useVoiceInput()

  const current = state.documents.find((d) => d.name === state.current) || null
  const referral = current?.referral || 'the team that publishes this document'
  const busy = !!state.replaying

  // Up next: the real section outline the navigator produced (title + the
  // same est_minutes spoken at a section transition), sliced to the ones
  // still ahead of the current position. [] until the navigator has run --
  // never a client-side guess.
  const curSectionIdx = current ? Math.max(1, current.progress.current_section_index) : 0
  const upNext = state.sections.slice(curSectionIdx, curSectionIdx + 3)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = document.activeElement?.tagName === 'INPUT'
      if (e.key === '/' && !typing) {
        e.preventDefault()
        inputRef.current?.focus()
      } else if (e.key === 'Escape') {
        e.preventDefault()
        s.interrupt()
        inputRef.current?.focus()
      } else if (e.code === 'Space' && !typing) {
        e.preventDefault()
        state.phase === 'playing' || state.phase === 'speaking' ? s.pause() : s.play()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [s, state.phase])

  const stopAndAsk = () => {
    s.interrupt()
    inputRef.current?.focus()
  }

  const submit = (e: React.FormEvent) => {
    e.preventDefault()
    if (!q.trim()) return
    // Enter while the voice is reading is a Stop-and-ask: the same interrupt
    // sequence goes first, so the boundary is the playhead and the reader is
    // stopped before the question is resolved. Otherwise the voice kept
    // reading over the answer.
    if (state.phase === 'playing' || state.phase === 'speaking') s.interrupt()
    s.ask(q.trim())
    setQ('')
  }

  useEffect(() => {
    // The welcome prompt's "upload" reply: the dialog opens for the listener.
    if (state.focusUpload > 0) dialogRef.current?.showModal()
  }, [state.focusUpload])

  useEffect(() => {
    // Keep the script panel's "now" row current as the read position moves,
    // or when the open document changes, while the panel is showing. The
    // ledger itself always comes from the server -- this only asks for it.
    if (showScript) s.getScript()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showScript, state.unit?.unitId, state.current])

  useEffect(() => {
    // A fresh document (or reopening the panel) starts back at one lazy
    // batch -- the previous document's scroll depth has nothing to do with
    // this one.
    setScriptVisible(SCRIPT_BATCH)
  }, [state.current, showScript])

  useEffect(() => {
    // Follow the read position: the "now" row scrolls into view as it
    // changes, so the panel never needs a manual scroll to see what's
    // playing right now. If lazy-loading hasn't reached that row yet,
    // reveal up to it first so there's something to scroll to.
    if (!showScript) return
    const nowIndex = state.script.findIndex((row) => row.status === 'now')
    if (nowIndex >= 0) {
      setScriptVisible((v) => Math.max(v, nowIndex + Math.ceil(SCRIPT_BATCH / 2)))
    }
    const id = requestAnimationFrame(() => {
      const el = scriptRef.current?.querySelector('.script-now')
      el?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    })
    return () => cancelAnimationFrame(id)
  }, [showScript, state.script])

  useEffect(() => {
    // Loads the next batch of clauses as the sentinel at the bottom of the
    // rendered list scrolls into view, instead of mounting the whole
    // document's worth of paragraphs up front.
    if (!showScript) return
    const root = scriptRef.current
    const sentinel = scriptSentinelRef.current
    if (!root || !sentinel) return
    const io = new IntersectionObserver(
      (entries) => {
        if (entries[0]?.isIntersecting) {
          setScriptVisible((v) => Math.min(v + SCRIPT_BATCH, state.script.length))
        }
      },
      { root, rootMargin: '200px' },
    )
    io.observe(sentinel)
    return () => io.disconnect()
  }, [showScript, state.script.length])

  const uploaded = (d: { name: string }) => {
    // It is in the library already (reviewed: false); open it and read.
    s.open(d.name)
    dialogRef.current?.close()
  }

  // Catalog (screen 1): the document already in progress gets its own
  // "Continue listening" card above the plain library list, exactly like the
  // screen map -- started, not finished, and not the placeholder currently
  // open in the reader for the first time.
  const continueListening = state.documents.find(
    (d) => d.progress.started && !d.progress.finished,
  )

  return (
    <div className={`listener${railOpen ? '' : ' rail-collapsed'}${showScript ? ' with-script' : ''}`}>
      <nav className="rail" aria-label="Your documents">
        <div className="rail-head">
          <h1 className="rail-title">Library</h1>
          <button
            className="rail-collapse-btn"
            onClick={toggleRail}
            aria-label={railOpen ? 'Collapse the document list' : 'Expand the document list'}
            aria-pressed={!railOpen}
            title={railOpen ? 'Collapse' : 'Expand'}
          >
            <ChevronLeft size={16} aria-hidden="true" />
          </button>
        </div>

        <button className="upload-cta" onClick={() => dialogRef.current?.showModal()}>
          <FilePlus size={16} aria-hidden="true" /> Upload a document
        </button>

        {continueListening && (
          <button
            className="continue-card"
            onClick={() => s.open(continueListening.name)}
            aria-label={`Continue listening to ${continueListening.spoken_title || continueListening.title}`}
          >
            <div className="continue-card-label">Continue listening</div>
            <div className="continue-card-title">{continueListening.spoken_title || continueListening.title}</div>
            <div className="continue-card-sub">{railSubtitle(continueListening)}</div>
            <div className="continue-card-bar">
              <div
                className="continue-card-fill"
                style={{
                  width: `${Math.min(
                    100,
                    Math.round(
                      (Math.max(1, continueListening.progress.current_section_index) /
                        Math.max(1, continueListening.section_count)) *
                        100,
                    ),
                  )}%`,
                }}
              />
            </div>
          </button>
        )}

        {state.documents.map((d) => (
          <div key={d.name} className="rail-item" style={{ position: 'relative' }}>
            <button
              className={`rail-row${d.name === state.current ? ' current' : ''}`}
              onClick={() => s.open(d.name)}
              aria-current={d.name === state.current ? 'true' : undefined}
            >
              <span className="t">
                {state.providerFellBack && d.name === state.current && (
                  <span className="dot amber" aria-label="Using the fallback voice" />
                )}
                {d.spoken_title || d.title}
              </span>
              <span className="s">{railSubtitle(d)}</span>
            </button>
            {d.reviewed === false && d.doc_id && (
              <button
                className="rail-trash"
                aria-label={`Delete ${d.spoken_title || d.title}`}
                title="Delete this document"
                style={{ position: 'absolute', right: 6, top: 6, background: 'none', border: 0, cursor: 'pointer', opacity: 0.7 }}
                onClick={(e) => {
                  e.stopPropagation()
                  if (window.confirm(`Delete "${d.spoken_title || d.title}"? Its files are removed.`)) s.remove(d.doc_id!)
                }}
              >
                🗑
              </button>
            )}
          </div>
        ))}

        <dialog ref={dialogRef} aria-label="Add a document">
          <p>Pick a PDF. It appears in your documents as soon as it is ready.</p>
          <UploadDocument face="listener" onDone={uploaded} />
          {state.sectionsFound.length > 0 && (
            <ul className="sections-found" aria-label="Sections found">
              {state.sectionsFound.map((t) => (
                <li key={t}>{t}</li>
              ))}
            </ul>
          )}
          {state.companionLines.length > 0 && (
            <div className="transcript" aria-label="Companion">
              {state.companionLines.map((l, i) => (
                <p key={i} className="notice">{l.text}</p>
              ))}
            </div>
          )}
          <form
            onSubmit={(e) => {
              e.preventDefault()
              if (!r.trim()) return
              s.reply(r.trim())
              setR('')
            }}
          >
            <input
              type="text"
              value={r}
              onChange={(e) => setR(e.target.value)}
              placeholder="Ask now; it is looked up first, once the document is ready"
              aria-label="Ask now"
            />
          </form>
          <button onClick={() => dialogRef.current?.close()}>Close</button>
        </dialog>
      </nav>

      {!railOpen && (
        <button
          type="button"
          className="library-toggle"
          onClick={toggleRail}
          aria-label="Show the document list"
          title="Show the document list"
        >
          <ChevronRight size={16} aria-hidden="true" /> Library
        </button>
      )}

      <main className="main">
       {state.answer && (
          <div className="interrupt-overlay" role="dialog" aria-label="Interruption">
            <section className="answer" aria-label="Answer">
              <span className="interrupted-tag">[interrupted]</span>
              <div className="q">
                <MessageSquare size={14} aria-hidden="true" /> You asked: {state.answer.question}
              </div>
              <div className="a">
                <CornerDownRight size={14} aria-hidden="true" /> {state.answer.answer}
              </div>
              <div className="disclaimer">
                Read from the document only. For a decision about your claim, contact {state.answer.referral}.
              </div>
            </section>

            <div className="resume-point">
              <div className="resume-point-label">Resume point</div>
              <div className="resume-point-text">
                {state.unit?.sectionTitle
                  ? `So — back to ${state.unit.sectionTitle}...`
                  : 'Ready to carry on where we left off.'}
              </div>
              <div className="actions">
                {state.answer.offer && (
                  <button
                    onClick={() => {
                      if (state.answer?.unitId) s.jump(state.answer.unitId)
                    }}
                  >
                    Jump there
                  </button>
                )}
                <button
                  className="primary"
                  onClick={() => {
                    // Keep going means read on: the reader was stopped by the
                    // question, so this has to start it, not just hide the card.
                    s.dispatch({ type: 'clearAnswer' })
                    s.play()
                  }}
                >
                  Keep going
                </button>
              </div>
            </div>
          </div>
        )}
       <div className="main-primary">
        {current?.unreviewed && (
          <div className="banner">
            Unreviewed document. It is loaded into this session only and is not in the library.
          </div>
        )}
        {state.error && <p className="notice">{state.error}</p>}

        <div className="reader-head">
          <div className="reader-head-left">
            <div>
              <div className="reader-doc-title">{current ? (current.spoken_title || current.title) : 'No document open'}</div>
              <div className="section-label">
                {state.unit
                  ? `${state.unit.sectionTitle}${state.unit.path ? ` · part ${state.unit.path}` : ''}`
                  : 'Not started'}
              </div>
            </div>
          </div>
          {current && current.section_count > 0 && (
            <div
              className="section-chip"
              aria-label={`Section ${Math.max(1, current.progress.current_section_index)} of ${current.section_count}`}
            >
              {Math.max(1, current.progress.current_section_index)}/{current.section_count}
            </div>
          )}
        </div>

        <div className="reader-card">
          <div className="doc">
            <Clause
              text={state.unit?.textDisplay ?? ''}
              boundary={state.boundaryChar}
              words={state.words}
              wordIndex={state.wordIndex}
              active={state.phase === 'playing'}
            />
          </div>
        </div>

        <StatusLine phase={state.phase} current={current} />

        {state.sectionsFound.length > 0 && state.topics.length === 0 && (
          <div className="coming-up notice" aria-label="Coming up">
            <strong>Coming up:</strong> {state.sectionsFound.slice(0, 8).join(' · ')}
            {state.sectionsFound.length > 8 ? ' · …' : ''}
          </div>
        )}

        {state.topics.length > 0 && state.prompt?.kind !== 'choice' && (
          <div className="chips" aria-label="Topics">
            {state.topics.map((t) => (
              <button key={t.topic} className="chip" onClick={() => s.topic(t.section_id)}>
                {t.topic}
              </button>
            ))}
          </div>
        )}

        {state.prompt?.kind === 'choice' && (
          <div className="actions" aria-label="Read it now or hear the overview first">
            <button onClick={() => s.choose('now')}>Read it now</button>
            <button onClick={() => s.choose('overview_first')}>Overview first</button>
          </div>
        )}

        {state.prompt?.kind === 'offer' && (
          <div className="actions" aria-label="Suggested question">
            <button onClick={() => s.reply('yes')}>Yes, answer that</button>
            <button onClick={() => s.reply('no')}>No, carry on</button>
          </div>
        )}

        {state.prompt?.kind === 'start_choice' && (
          <div className="actions" aria-label="A topic, the brief, or from the start">
            <button onClick={() => s.reply('brief')}>Brief</button>
            <button onClick={() => s.reply('from the start')}>From the start</button>
          </div>
        )}

        {state.prompt?.kind === 'confirm_topic' && (
          <div className="actions" aria-label={`Read ${state.prompt.heading ?? 'it'} now?`}>
            <button onClick={() => s.reply('yes')}>Yes, read it</button>
            <button onClick={() => s.reply('no')}>No</button>
          </div>
        )}

        {state.prompt?.kind === 'welcome' && (
          <div className="actions" aria-label="Which document, or upload a new one">
            {(state.prompt.titles || []).map((t) => (
              <button key={t} className="chip" onClick={() => s.reply(t)}>
                {t}
              </button>
            ))}
            <button onClick={() => s.reply('upload')}>Upload</button>
          </div>
        )}

        {state.prompt?.kind === 'pick_topic' && (
          <div className="actions" aria-label="Where to start">
            {(state.prompt.topics || []).map((t) => (
              <button key={t} className="chip" onClick={() => s.reply(t)}>
                {t}
              </button>
            ))}
            <button onClick={() => s.reply('from the top')}>From the top</button>
          </div>
        )}

        {(state.prompt?.kind === 'section_end' || state.prompt?.kind === 'not_found') && (
          <div className="actions" aria-label="Carry on, or something else">
            <button onClick={() => s.reply('carry on')}>Carry on</button>
          </div>
        )}

        {state.prompt?.kind === 'end_choice' && (
          <div className="actions" aria-label="A section again, a recap, or stop">
            <button onClick={() => s.reply('recap')}>Recap</button>
            <button onClick={() => s.reply('stop')}>Stop</button>
          </div>
        )}

        {state.prompt?.kind === 'ingest_wait' && (
          <p className="notice">Ask now and it is looked up first, once the document is ready.</p>
        )}

        {state.prompt?.kind === 'table_choice' && (
          <div className="actions" aria-label="A row of the table, all of them, or carry on">
            {(state.prompt.labels || []).map((lab) => (
              <button key={lab} className="chip" onClick={() => s.reply(lab)}>
                {lab}
              </button>
            ))}
            <button onClick={() => s.reply('all of them')}>All</button>
            <button onClick={() => s.reply('carry on')}>Carry on</button>
          </div>
        )}

        {upNext.length > 0 && (
          <div className="up-next" aria-label="Up next">
            <div className="up-next-head">
              <span>Up next</span>
              {current && (
                <span className="muted">about {current.progress.minutes_left} min left</span>
              )}
            </div>
            {upNext.map((sec) => (
              <div className="up-next-row" key={sec.id}>
                <span>{sec.title}</span>
                {sec.est_minutes != null && <span className="muted">{sec.est_minutes} min</span>}
              </div>
            ))}
          </div>
        )}

        <form onSubmit={submit} className="ask-row">
          <input
            ref={inputRef}
            type="text"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Ask about what you just heard"
            aria-label="Ask about what you just heard"
          />
          <button type="button" onClick={stopAndAsk} disabled={busy} aria-label="Stop and ask">
            <Hand size={ICON} aria-hidden="true" /> Stop and ask
          </button>
        </form>

        <div className="transport">
          <button
            className="round-button"
            onClick={s.skipBack}
            disabled={busy || current?.readable === false}
            aria-label="Previous section"
          >
            <ChevronLeft size={ICON} aria-hidden="true" />
          </button>
          <button
            className="round-button primary"
            onClick={() => (state.phase === 'playing' || state.phase === 'speaking' ? s.pause() : s.play())}
            disabled={busy || current?.readable === false}
            aria-label={state.phase === 'playing' || state.phase === 'speaking' ? 'Pause' : 'Play'}
          >
            {state.phase === 'playing' || state.phase === 'speaking' ? <Pause size={ICON} aria-hidden="true" /> : <Play size={ICON} aria-hidden="true" />}
          </button>
          <button
            className="round-button"
            onClick={s.skipForward}
            disabled={busy || current?.readable === false}
            aria-label="Next section"
          >
            <ChevronRight size={ICON} aria-hidden="true" />
          </button>
          <button
            className={`round-button mic${voice.status === 'live' ? ' mic-live' : ''}`}
            onClick={voice.toggle}
            disabled={voice.status === 'connecting'}
            aria-pressed={voice.status === 'live'}
            title={
              voice.status === 'live'
                ? 'Voice input is on — click to stop'
                : voice.status === 'error'
                  ? `Voice input unavailable: ${voice.error}`
                  : 'Talk instead of typing'
            }
            aria-label="Voice input"
          >
            <Mic size={ICON} aria-hidden="true" />
          </button>
        </div>
        <div className="transport-footer">
          <span className="muted">{voice.status === 'live' ? 'Listening…' : ''}</span>
          <button
            type="button"
            className="script-toggle"
            onClick={() => {
              const next = !showScript
              setShowScript(next)
              if (next) s.getScript()
            }}
            aria-pressed={showScript}
          >
            <FileText size={16} aria-hidden="true" /> {showScript ? 'Hide script' : 'Show script'}
          </button>
        </div>
        {state.heardAs && (
          <p className="notice" aria-label="Heard as">
            Heard as: {state.heardAs.intent}
            {state.heardAs.sectionTitle ? ` ${state.heardAs.sectionTitle}` : ''}
          </p>
        )}
        {busy && <p className="notice">Replaying a recorded session. Playback is paused.</p>}
        {!state.audioSink && state.anySink && (
          <p className="notice">The voice is playing in another tab. Press play here to move it.</p>
        )}
        {voice.status === 'error' && (
          <p className="notice">Voice input isn&apos;t available right now: {voice.error}</p>
        )}
       </div>
      </main>

      {showScript && (
        <div className="script-panel" aria-label="Script" ref={scriptRef}>
          <div className="script-panel-head">
            <div className="script-caption">
              Grey = heard &middot; yellow = playing now &middot; faint = not sent yet
            </div>
            <button
              className="script-close"
              onClick={() => setShowScript(false)}
              aria-label="Hide script"
              title="Hide script"
            >
              <X size={14} aria-hidden="true" />
            </button>
          </div>
          {state.script.length === 0 ? (
            <p className="notice">Nothing has been read yet.</p>
          ) : (
            <>
              {state.script.slice(0, scriptVisible).map((row, i) => (
                <p key={i} className={`script-row script-${row.status}`}>
                  {row.text}
                </p>
              ))}
              {scriptVisible < state.script.length && (
                <div ref={scriptSentinelRef} className="script-sentinel" aria-hidden="true" />
              )}
            </>
          )}
        </div>
      )}
    </div>
  )
}

function StatusLine({ phase, current }: { phase: string; current: DocEntry | null }) {
  if (phase === 'finished') {
    return <p className="statusline">That&rsquo;s the end of the document.</p>
  }
  if (phase === 'playing') {
    const sec = Math.max(1, current?.progress.current_section_index ?? 1)
    return (
      <p className="statusline">
        <Volume2 size={16} aria-hidden="true" /> Reading section {sec} of {current?.section_count ?? 1}
      </p>
    )
  }
  if (phase === 'answering') {
    return (
      <p className="statusline">
        <MessageSquare size={16} aria-hidden="true" /> Answering from the document&hellip;
      </p>
    )
  }
  if (phase === 'speaking') {
    return (
      <p className="statusline">
        <Volume2 size={16} aria-hidden="true" /> Reading you the answer.
      </p>
    )
  }
  if (phase === 'resuming') {
    return (
      <p className="statusline">
        <CornerDownRight size={16} aria-hidden="true" /> Picking up where you were.
      </p>
    )
  }
  if (phase === 'paused') {
    return (
      <p className="statusline">
        <Pause size={16} aria-hidden="true" /> Paused here. Ask a question, or press play to continue.
      </p>
    )
  }
  return <p className="statusline">Press play to begin.</p>
}

/**
 * Heard text is ink, unheard is grey, and the boundary is a rule at the exact
 * character the server derived from the audio clock. Text past the boundary
 * stays grey even while it is already in the browser.
 */
export function Clause({
  text,
  boundary,
  words,
  wordIndex,
  active,
}: {
  text: string
  boundary: number
  words: { words: string[]; start: number[]; end: number[] } | null
  wordIndex: number
  active: boolean
}) {
  if (!text) return <p className="unheard">Nothing is being read yet.</p>
  const cut = Math.max(0, Math.min(boundary, text.length))
  const heard = text.slice(0, cut)
  const rest = text.slice(cut)

  let heardNode: React.ReactNode = heard
  if (active && words && wordIndex >= 0 && wordIndex < words.words.length) {
    const w = words.words[wordIndex]
    const at = heard.lastIndexOf(w)
    if (at >= 0) {
      heardNode = (
        <>
          {heard.slice(0, at)}
          <span className="playing-word">{w}</span>
          {heard.slice(at + w.length)}
        </>
      )
    }
  }

  return (
    <p>
      <span className="heard">{heardNode}</span>
      {cut < text.length && <span className="boundary" aria-label="How far you have heard" />}
      <span className="unheard">{rest}</span>
    </p>
  )
}
