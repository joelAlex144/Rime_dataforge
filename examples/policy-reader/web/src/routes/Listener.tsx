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
  CornerDownRight,
  FilePlus,
  Hand,
  MessageSquare,
  Mic,
  Pause,
  Play,
  Volume2,
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
  const voice = useVoiceInput()

  const current = state.documents.find((d) => d.name === state.current) || null
  const referral = current?.referral || 'the team that publishes this document'
  const busy = !!state.replaying

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

  const uploaded = (d: { name: string }) => {
    // It is in the library already (reviewed: false); open it and read.
    s.open(d.name)
    dialogRef.current?.close()
  }

  return (
    <div className="listener">
      <nav className="rail" aria-label="Your documents">
        <h2>Your documents</h2>
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
        <button className="rail-row" onClick={() => dialogRef.current?.showModal()}>
          <span className="t">
            <FilePlus size={16} aria-hidden="true" /> Add a document
          </span>
        </button>

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

      <main className="main">
        {current?.unreviewed && (
          <div className="banner">
            Unreviewed document. It is loaded into this session only and is not in the library.
          </div>
        )}
        {state.error && <p className="notice">{state.error}</p>}

        <div className="section-label">
          {state.unit
            ? `${state.unit.sectionTitle}${state.unit.path ? ` · part ${state.unit.path}` : ''}`
            : current
              ? current.title
              : 'No document open'}
        </div>

        <div className="doc">
          <Clause
            text={state.unit?.textDisplay ?? ''}
            boundary={state.boundaryChar}
            words={state.words}
            wordIndex={state.wordIndex}
            active={state.phase === 'playing'}
          />
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

        {state.answer && (
          <section className="answer" aria-label="Answer">
            <div className="q">You asked: {state.answer.question}</div>
            <div className="a">{state.answer.answer}</div>
            <div className="disclaimer">
              Read from the document only. For a decision about your claim, contact {state.answer.referral}.
            </div>
            {state.answer.offer && (
              <div className="actions">
                <button
                  onClick={() => {
                    if (state.answer?.unitId) s.jump(state.answer.unitId)
                  }}
                >
                  Jump there
                </button>
                <button
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
            )}
          </section>
        )}

        <div className="transport">
          <button
            className="primary"
            onClick={() => (state.phase === 'playing' || state.phase === 'speaking' ? s.pause() : s.play())}
            disabled={busy || current?.readable === false}
            aria-label={state.phase === 'playing' || state.phase === 'speaking' ? 'Pause' : 'Play'}
          >
            {state.phase === 'playing' || state.phase === 'speaking' ? <Pause size={ICON} aria-hidden="true" /> : <Play size={ICON} aria-hidden="true" />}
            {state.phase === 'playing' || state.phase === 'speaking' ? 'Pause' : 'Play'}
          </button>
          <button onClick={stopAndAsk} disabled={busy} aria-label="Stop and ask">
            <Hand size={ICON} aria-hidden="true" /> Stop and ask
          </button>
          <form onSubmit={submit} style={{ display: 'contents' }}>
            <input
              ref={inputRef}
              type="text"
              value={q}
              onChange={(e) => setQ(e.target.value)}
              placeholder="Ask about what you just heard"
              aria-label="Ask about what you just heard"
            />
          </form>
          <button
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
            {voice.status === 'live' ? ' Listening…' : ''}
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
      </main>
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
