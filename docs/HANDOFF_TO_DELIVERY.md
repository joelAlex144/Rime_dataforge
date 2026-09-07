# Synthesis-side handoff — answers, and what changed in your code

Reply to `docs/HANDOFF_TO_SYNTHESIS.md`. Everything below is how the code stands
after integration, not a plan.

---

## Your four open questions

### 1. Timestamp anchor — your assumption was wrong, and it is now fixed

You built against **t=0 means "synth requested"**. Measured against Rime, t=0 is
the **first audio sample of the unit**. Both Rime's word timestamps and the
worklet's `rendered_ms` are audio-clock values, so they are directly comparable
— which is what your boundary resolution needs — but only under the corrected
anchor. Anchoring word timings at the request would have offset every comparison
by the time to first byte, a drift that grows with provider latency and never
raises an error.

Corrected in three places: the convention block at the top of
`playback_protocol.py`, the boundary-resolution docstring in `ledger.py`, and
`tts/tracked.py`, which derives every chunk's `t_start_ms`/`t_end_ms` from
cumulative bytes so it sits on that clock by construction.
`tests/test_protocol.py::TestAnchorConvention` fails if the old wording returns.

### 2. Word-map alignment target — confirmed, aligned to `text_display`

`WordTiming.char_start` / `char_end` index `text_display`, exactly as you
assumed. The alignment runs through the normalizer's span map: Rime is sent
`text_spoken` ("Section four b two"), and `delivery_layer/wordmap.py` maps its
tokens back onto the display form ("Section 4(b)(ii)"). Pinned by
`tests/test_tracked.py::TestWordAlignment::test_section_reference_spans_its_three_spoken_tokens`.

One addition you should know about: a **resumed** unit carries `char_start > 0`,
and its word timings are reported in the **original** clause's coordinates, not
the fragment's. So a `WordTiming` on `sec-4b-vii/resume#3` with `char_start=124`
means character 124 of `sec-4b-vii`.

### 3. Fixture/unit schema — different from your placeholder; the loader is updated

Not a flat list. It is:

```json
{"title": "...", "clause_count": 213,
 "clauses": [{"id": "sec-4b-vii", "index": 46, "section": 4, "subsection": "b",
              "item": "vii", "section_title": "Perils Insured Against",
              "text_display": "...", "text_spoken": "...",
              "sentences": [[0,123],[124,228]],
              "spoken_map": [[a,b,"spoken"], ...]}]}
```

`spoken_map` and `sentences` are load-bearing: without the first there is no word
map, and without the second the resume anchor falls back to a punctuation
heuristic that misfires on currency and section references.
`fixtures/index.json` lists the available documents; there are two, a synthetic
213-clause policy and a 45-clause GOV.UK page.

`load_units()` reads this and still accepts your flat list, so
`tests/fixture_demo.json` keeps working. `clause_label` is now a spoken citation
for numbered clauses ("Section 4(b)(vii), Perils Insured Against") and the bare
heading for unnumbered documents, where a section number would be meaningless
read aloud.

### 4. `provider_active` ownership — the adapter owns it, single writer

Removed from `agent.py`. The provider emits it on `connect()`, because it is the
only thing that knows the real model, speaker, endpoint, format and sample rate.
Verified in the demo trace: exactly one, and on the Rime path it reads
`coda / bancroft / en / pcm / 24000`.

---

## Two more of your assumptions that did not survive contact

**"One `Timestamps` per `synth()`, before any audio."** Rime emits `timestamps`
**per segment, interleaved with audio chunks**. `TrackedTTS` accumulates them and
re-emits a cumulative map each time new words arrive, so each emission replaces
the previous one. `Ledger.register_word_map` is therefore called several times
per unit and last-write-wins is deliberate. At interrupt time the newest map may
be partial; that is correct, and resolution stays conservative because a word
with no timing cannot be counted as delivered.

**"`cancel()` allows exactly one more chunk."** At the iterator level our
`cancel()` ends the stream with **zero** stragglers. At the wire level they are
**unbounded** — preflight measured **23 s of audio arriving after `clear`**,
1112576 bytes, fenced across 1181 events. Your fence is the right shape and is
what makes this safe; "normal" just looks bigger than you expected. Our
`tts/fake.py` gained `straggle: int = 1` so the path is exercisable offline;
yours is deleted.

---

## Changes made to your files

Every one is a wiring change; none rewrites your fence, ledger, position or
scheduler logic.

| File | Change | Why |
|---|---|---|
| `ledger.py` | takes an `EventLog`; `_write` delegates to it | one file holds provider and delivery events on one clock |
| `ledger.py` | `resolve()` accepts both `type` and `event` keys | your older committed traces still resolve |
| `ledger.py` | Done + `rendered_ms >= duration` marks heard | timings overshoot audio by ~100 ms, leaving a completed unit a word short |
| `ledger.py` | `register_resume` / `delivered_char_end` | links a resumed fragment to its original, and exposes the boundary the resume anchor is computed from |
| `scheduler.py` | `MAX_IN_FLIGHT` 3 to 2 | every extra in-flight unit is more audio to fence after the 23 s leak |
| `scheduler.py` | passes the whole unit to `synth()`; `Unit` gained `spoken_map`, `sentences`, `char_start` (all defaulted) | the adapter cannot align to `text_display` without the span map |
| `scheduler.py` | sync cancels go via `cancel_nowait` | our cancel sends on a socket, so it is a coroutine |
| `agent.py` | no `provider_active`; uses `make_provider` + `TrackedTTS` | your question 4 |
| `agent.py` | `load_units` reads the real fixture | your question 3 |
| `agent.py` | `GroundingAnswerProvider` | grounding was unowned; it is ours, and it is wired |
| `agent.py` | mid-unit resume | your flagged gap: resume no longer restarts at the top of a unit |
| `agent.py` | `read_cursor_order` computed as an index | it was set from `Unit.order`; those coincide only when the list starts at order 0 |
| `playback_protocol.py` | v2, `UnitStart.char_start` | so the client can show a resumed fragment in place |
| `client/sdk/client.js` | reads `char_start` | same |
| `tests/demo_interrupt_cycle.py` | real fixture, `SimulatedClient` | with nothing acking, every unit correctly resolves `never_played` and no boundary exists |
| `.gitignore` | no blanket `traces/*` ignore | traces are the evidence artifact `RIME_EVIDENCE.md` cites |

---

## Must not change

- **`unit_skipped` is a ledger value now.** `EventType.UNIT_SKIPPED`
  (`"unit_skipped"`) was added to `ledger.py` and to the event list in
  `delivery_layer/events.py`. The synthesis side's structure pass marks page
  furniture and table rows as `kind: boilerplate` / `spoken_on_request`; the
  reader writes one `unit_skipped` per such clause at session start with
  `reason: boilerplate | table_on_request`. It is a fourth terminal state next
  to heard / truncated / never_played, so a session record accounts for every
  clause in the fixture. Your resolver can ignore it; nothing else changed.
- **Heard is only ever an ack.** `frames_played` / `audible_stop` from the
  worklet's rendered-sample counter. Nothing may mark a unit heard because
  synthesis finished.
- **The audio-sample anchor.** Both halves now agree; reverting either side
  silently drifts every boundary.
- **`char_start` on a resumed unit.** Dropping it makes the ledger record
  fragment-relative offsets that look like valid original-clause offsets.
- **Word maps aligned to `text_display`.**

## Should not change without telling the other side

- `MAX_IN_FLIGHT = 2`.
- Last-write-wins on `register_word_map`.
- The `<unit_id>/resume#<turn>` id shape — the acceptance harness matches on it.

---

## Still open

- **LiveKit room join and the `livekit-agents` entrypoint.** Still the documented
  stub. Needs credentials and a pinned version; not attempted.
- **STT.** `SpeechToText` is still an interface with a demo stand-in.
- **A1 (far-end audible stop) and A2 (ASR agreement).** These need a WAV
  recording of the browser client's speaker output.
  `examples/policy-reader/acceptance/measure_far_end.py` is scaffolded for it;
  making the recording is a manual step.
- **Two `result_fenced` writers.** `fence.check()` logs one shape (issued and
  current turn ids, no byte count) and the provider logs another (byte counts,
  no turn ids). Both are useful; they should probably converge on one shape.

## Document upload

Upload is a first-class path on the same server, not a `--dev` feature, and it
touches your UI and your server. The contract:

- `POST /documents` -- multipart `file` (PDF; `.docx`, `.txt`, `.md`, `.html`
  accepted through the same path) or a JSON body `{"url": ...}`. The response
  is `text/event-stream`: one event per pipeline stage,
  `{"stage": "extract"|"structure"|"segment"|"normalize"|"pii_scan"|"validate"|"write",
  "status": "ok", "elapsed_ms": n, "detail": "..."}`, then a final
  `{"stage": "done", "status": "ok", "entry": {doc_id, name, title, reviewed,
  readable, clause_count, report}}`. On failure the final event is
  `{"stage": "done", "status": "error", "error": "..."}`. Uploading the same
  bytes again returns a single `done` event with `"existing": true` and the
  same entry. `413` for more than 25 MB and `415` for an unsupported type are
  the only HTTP errors; everything else is accepted.
- `GET /documents/<doc_id>/report` -- the ingest report (JSON).
- `POST /documents/<doc_id>/accept` -- sets `reviewed: true`; returns the entry.
- The websocket broadcasts `library_changed` with the listener library after
  an upload or an accept.

**The listener page must not surface report contents.** It renders a progress
bar from the stage events, the elapsed time and the title when the entry
arrives -- no stage names, no clause counts, no scan findings, no accept step.
The developer page renders the stage list, the report in full and Accept.
Identity is `(doc_id, clause_id)`; `doc_id` is a content hash of the source.

## Mismatch #11 — jump must go through the interruption path

Observed: after an interruption, "Jump there" (or "go to claims") did not move
the reader; the queued lookahead unit played and the position manager restored
the old read position afterwards. Cause: jump set the reference position but
not the read cursor, and cancelled nothing in flight.

Every jump -- topic chip, spoken topic name, the spoiler-gate offer, "skip it",
"go back to where I was" -- is now the same six-step interruption with a
different resume target:

1. `turn_id++`, as for VAD.
2. Cancel fan-out: Rime `clear`, scheduler stop, worklet flush ->
   `audible_stop_ts`, `cancel_issued`. From the tab with the voice the client
   flushes first and sends `flush_ack`, then the jump; from another tab the
   server sends `flush` to the sink and waits for its ack.
3. Ledger truncates the sounding unit at the client-confirmed boundary
   (`unit_truncated`, reason `jump:<why>`). Every readable unit between the cut
   and the target is written `unit_skipped` with `reason: jump`.
4. `position_saved` for the old read position, labelled `before_jump`, so "go
   back to where I was" returns to it. Read cursor := sentence start of the
   target clause, never unit top. `position_restored` with `reason: jump`.
5. A cue unit is synthesised first, from a template: "Okay — going to
   {heading}." followed by the section brief when the fixture has one. Its own
   unit, its own ledger entry (`cue_spoken`, context `cue#t<n>`).
6. Resume at the target.

`jump` event `{from_unit, to_unit, reason, turn_id}` is written once per jump
(`EventType.JUMP`). Ownership: steps 1–4 are `scheduler.py` / `position.py` /
the ledger; target resolution (`grounding.py`: `find_section`,
`navigation_intent`) and the cue template are this side. The reference
implementation is `ReaderSession.jump_to` in `examples/policy-reader/server.py`,
covered by `tests/test_server_jump.py`.

### UI hooks (navigator)

- `topics` `{topics: [{topic, section_id, heading}]}` is broadcast when the
  overview starts; the client shows them as chips while it plays. A tap sends
  `{"type": "topic", "section_id"}` (`section_id: null` = "Read from the start");
  from the tab with the voice, `flush_ack` first.
- `choice` `{options: ["now", "overview_first"], section_id}` follows the spoken
  "{Topic} is {heading}, about N minutes. Read it now, or hear the rest of the
  overview first?"; the client answers `{"type": "choice", "choice": "now" |
  "overview_first"}`, or the listener says "now" / "overview first". Silence for
  8 s: "I'll read from the start. Interrupt me any time." and reading starts.
- Section transitions are spoken as the heading unit: "Next is {heading}.
  {brief} About N minutes." "skip it" jumps to the next section; "go on" (or
  nothing) continues.
- `offer` `{question, clause_id}` follows the spoken "People usually ask here
  whether … Want that?" after a section is heard; "yes" answers from the stored
  clause (`retrieval_path: suggested`, no retrieval); "no" or 6 s of silence
  continues (`offer_closed`).
- Extractive asks -- "read me every exclusion", "what deadlines are in this
  document", "summarise this section" -- are answered from the fixture's tags
  and briefs with citations (`retrieval_path: extractive`), never ranked, no
  model.
- The listener page must not surface enrichment provenance; the developer page
  shows `GET /documents/<doc_id>/enrichment` (provider, model, elapsed, guard
  rejections, generated fields). The existing Accept covers generated text.
