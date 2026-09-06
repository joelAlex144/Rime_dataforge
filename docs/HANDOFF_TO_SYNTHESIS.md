# Delivery-side handoff — for the synthesis-side integration

This documents what's built and tested on the delivery/client side, the exact
interface your `tts/rime.py` needs to satisfy to plug in, what's still open,
and how to run the existing test/demo against your work once it lands.

---

## What's done (built + tested)

All files below live at the repo root (per the project's repo-shape rule: the
delivery-aware layer is the top-level product, `examples/policy-reader/` is
the demo environment).

| File | What it does | Tested how |
|---|---|---|
| `playback_protocol.py` | Versioned wire protocol, both directions (server→client: `UnitStart`/`AudioChunk`/`WordTimestamps`/`UnitDone`/`Cancel`; client→server: `PlaybackAck`/`FlushAck`/`ClientError`). JSON encode/decode helpers. | Round-trip encode/decode for every message type. |
| `fence.py` | Monotonic `turn_id` counter + `check()` called at every await boundary. Drops stale results, logs `result_fenced`. Raises `StaleGeneration`. | Nested-interruption scenario in `scheduler.py` tests. |
| `ledger.py` | Append-only JSONL event log (all 9 event types incl. `provider_active`). Boundary resolution: given `rendered_ms` + a word map, finds last fully-delivered word, flags a straddling word, marks unacked units `never_played`. Derives `session_record.json`. | Straddling-word case, never-played case, full scheduler integration — all verified with exact expected output. |
| `position.py` | Position **stack** (not a single variable) for nested interruptions. Sentence-boundary resume anchor resolution. Deictic target tracking (`that`/`it` resolves to last-heard clause). Re-entry cue templates. | Nested push/pop cycle, sentence-boundary heuristic against real punctuation cases. |
| `scheduler.py` | Pulls units in order, caps in-flight synth at 2–3, tags every chunk `(turn_id, unit_id, seq)`, stops cleanly on cancel. Depends only on a **structural** `TTSBackend` protocol — never imports a concrete TTS module. | Full run to completion; mid-run cancel that correctly truncates the in-flight unit and fences out not-yet-started units. |
| `tts/fake.py` | Stand-in for your `tts/rime.py`. Same interface (below). Sine tone + synthetic word timings. Deliberately emits one straggler chunk after `cancel()` to exercise fencing realistically. | Full synth run; cancel-mid-stream (caught and fixed a real bug where cancellation never actually took effect). |
| `client/worklet/playback-processor.js` | AudioWorklet: per-unit PCM queue, samples-**rendered** counter (not samples-enqueued), ~100ms acks, flush drops queue + immediate ack with `audible_stop_ts` from the audio-context clock. | Logic-tested with a stubbed AudioWorklet environment in Node (no browser needed) — 4 scenarios incl. flush timing and full-turn flush. |
| `client/sdk/client.js` | LiveKit room join, mic up, data-channel (de)serialization matching `playback_protocol.py` exactly, forwards worklet acks upstream. | Syntax-checked; not yet run against a live LiveKit room (needs credentials). |
| `examples/policy-reader/agent.py` | Wires fence/ledger/position/scheduler into a session: VAD speech-start → bump turn → cancel fan-out (scheduler stop + client flush broadcast) → STT → answer spoken through the **same** tracked pipeline → resume. | Full end-to-end interrupt→answer→resume cycle run against `tts/fake.py`, ledger inspected and confirmed correct. |
| `tests/demo_interrupt_cycle.py` | Runnable, no-external-services demo of the full cycle above. | This *is* the test — run it yourself: `python tests/demo_interrupt_cycle.py`. |

---

## The interface your `tts/rime.py` must satisfy

This is what `scheduler.py` and `tts/fake.py` currently agree on. `tts/base.py`
(your file) should formalize this; until it exists, `tts/fake.py` defines it
locally and is clearly marked as provisional.

```python
async def synth(self, text: str, context_id: str) -> AsyncIterator[AudioChunk | Timestamps | Done]:
    ...

def cancel(self, context_id: str) -> None:
    ...
```

Where (see `tts/fake.py` for the exact dataclasses):

- `AudioChunk(context_id, chunk_index, pcm_b64, t_start_ms, t_end_ms)` — base64
  little-endian int16 PCM, mono, 24kHz (matches `playback_protocol.py`'s
  `UnitStart.sample_rate_hz`).
- `Timestamps(context_id, words: tuple[WordTiming, ...])` — one `Timestamps`
  event per `synth()` call, before any audio.
- `Done(context_id, total_duration_ms)` — only emitted if not cancelled.
- `WordTiming(word, t_start_ms, t_end_ms, char_start, char_end)`.

**`cancel()` semantics assumed by the fence/ledger:** best-effort, not
instant — the fake deliberately allows exactly one more chunk to arrive after
`cancel()` is called (simulating real network/buffering slop), then stops
unconditionally. If your real Rime adapter's `/ws3` cancellation behaves
differently (e.g. guarantees zero further events, or can straggle more than
one chunk), **tell me** — `fence.py`'s stale-result handling is the safety
net regardless, but it changes what "normal" vs "exceptional" looks like in
the ledger.

---

## Open questions — need your input before final wiring

1. **Timestamp anchor.** I built everything assuming unit-local `t=0` means
   "synth requested," not "first byte received." If your real Rime adapter
   anchors differently, every `rendered_ms` comparison in `ledger.py`'s
   boundary resolution silently drifts. Please confirm or correct.
2. **Word-map alignment target.** `ledger.py`'s boundary resolution assumes
   word tuples are aligned to `text_display` (what gets read back / shown),
   not `text_spoken` (what Rime actually vocalizes — e.g. "four b two" vs
   "4(b)(ii)"). Confirm your word map does this alignment before it reaches
   my side.
3. **Fixture/unit schema.** `agent.py`'s `load_units()` currently expects:
   ```json
   {"unit_id": "sec-4b-ii", "order": 0, "text_display": "...", "text_spoken": "...", "clause_label": "the exclusions clause"}
   ```
   as a flat JSON list. If your real fixture differs, tell me and I'll adjust
   the loader (it's a 10-line function, not a big change) — but the sooner
   this is locked the sooner we can test against the real document instead
   of my 3-clause placeholder.
4. **`provider_active` ownership.** Right now `agent.py` logs this once at
   session start with `provider="fake"`. Once your adapter is live, this
   should flip to `"rime"` (or log a disclosed fallback if Rime is
   unavailable) — happy to have this driven from your config rather than
   hardcoded, just needs a single source of truth so the ledger doesn't get
   two writers disagreeing.

---

## What's still open on my side (not blocking you)

- Real LiveKit Agents entrypoint wiring (`agent.py`'s `entrypoint()` is a
  documented stub — needs a pinned `livekit-agents` version to build against).
- Real STT and Q&A grounding are **not built** — `agent.py` exposes narrow
  `SpeechToText` / `AnswerProvider` interfaces with a safe placeholder.
  Ownership of grounding wasn't explicit on either of our lists — worth
  deciding who builds it.
- Mid-unit resume by exact character offset isn't implemented — resume
  currently restarts from the top of a unit, not a precise offset inside one.
  Flagged inline in `agent.py`.

---

## How to test against your work once `tts/rime.py` lands

`tests/demo_interrupt_cycle.py` currently hardcodes `FakeTTS` via
`agent.py`'s `PROVIDER_NAME = "fake"`. Once your adapter exists:

1. Swap `from tts.fake import FakeTTS` → `from tts.rime import RimeTTS` in
   `PolicyReaderSession.__init__` (one line), flip `PROVIDER_NAME = "rime"`.
2. Run `python tests/demo_interrupt_cycle.py` — same test, real voice.
3. Inspect `traces/demo_ledger.jsonl` and `traces/demo_session_record.json` —
   this is the same evidence format the acceptance test / `RIME_EVIDENCE.md`
   will be built on.

Repo: https://github.com/astitva-exe-23/inseedo_dataforge
