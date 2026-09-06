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

const ICON = 18

function railSubtitle(d: DocEntry): string {
  if (d.readable === false) return 'No readable text found'
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
          <button
            key={d.name}
            className={`rail-row${d.name === state.current ? ' current' : ''}`}
            onClick={() => s.open(d.name)}
            aria-current={d.name === state.current ? 'true' : undefined}
          >
            <span className="t">
              {state.providerFellBack && d.name === state.current && (
                <span className="dot amber" aria-label="Using the fallback voice" />
              )}
              {d.title}
            </span>
            <span className="s">{railSubtitle(d)}</span>
          </button>
        ))}
        <button className="rail-row" onClick={() => dialogRef.current?.showModal()}>
          <span className="t">
            <FilePlus size={16} aria-hidden="true" /> Add a document
          </span>
        </button>

        <dialog ref={dialogRef} aria-label="Add a document">
          <p>Pick a PDF. It appears in your documents as soon as it is ready.</p>
          <UploadDocument face="listener" onDone={uploaded} />
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
          <button disabled title="Voice input arrives with the LiveKit client" aria-label="Voice input">
            <Mic size={ICON} aria-hidden="true" />
          </button>
        </div>
        {busy && <p className="notice">Replaying a recorded session. Playback is paused.</p>}
        {!state.audioSink && state.anySink && (
          <p className="notice">The voice is playing in another tab. Press play here to move it.</p>
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
