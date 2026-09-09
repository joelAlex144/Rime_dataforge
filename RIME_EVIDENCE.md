# RIME_EVIDENCE.md

## Hard voice claim

**Interruption and recovery over a Rime + LiveKit + own-orchestration stack.** After any interruption, the agent's state contains only the text the client confirmed as played, truncated at the delivery boundary; mid-read questions resolve against the last clause *heard*, not the last clause *sent*; reading resumes within one sentence of the cut.

Supporting: **evaluation and observability** — every session emits an append-only event log from which "what did the listener actually hear" can be reconstructed.

Not claimed: novelty on "track what the user heard" (Azure Voice Live and Deepgram Flux ship it inside their own platforms). Claimed: the composed-stack implementation on a pure TTS provider, plus positional resume and deictic resolution on top.

## Acceptance test (written before the demo)

Fixture: `examples/policy-reader/fixtures/policy.json`, 213 synthetic clauses, stable ids.
Script: 20 interruption points at known clause/word offsets (`traces/acceptance_script.json`, Person B).

Per trial, assert:

| # | Assertion | Source of truth |
|---|---|---|
| A1 | `audible_stop_latency` measured at the far end of the transport; report p50 and p95 | client audio clock, `audible_stop` event |
| A2 | `delivered_text` in the ledger == ASR of captured audio (character-level agreement) | `unit_truncated.char_end` vs Whisper on the recorded clip |
| A3 | "what does that mean" resolves to the correct last-heard clause id | `grounding.resolve()` result vs script |
| A4 | `resume_position` within one sentence of `cut_position` | `position_restored` vs `unit_truncated` |
| A5 | no clause marked delivered that was never played | ledger vs `frames_played` |

Unverified numbers get no credit. Cached vs uncached runs are labelled separately.

## Method — Rime path

All Rime evidence is produced by committed scripts against the exact shipped configuration (`README.md` → *Rime integration*). Each script writes an event log to `traces/` with the provider descriptor (`provider_active`) as its first record.

### M1. Live-catalog verification — `scripts/fetch_voices.py`
Fetches the catalog at run time, asserts the configured `speaker` exists for `modelId` and `lang`, and commits the snapshot (`traces/rime_catalog_<date>.json`, `traces/rime_catalog_check.json`). Re-run on submission day; the check date is in the file.

### M2. Preflight of the shipped path — `scripts/preflight_rime.py --clear`
Opens `/ws3` with the shipped query string, synthesises clause `sec-4b-vii`, and asserts: PCM arrived with no WAV header; timestamps arrived, index-aligned and monotonic; audio duration from byte count agrees with the last word end within 1.5 s; the word map built from those timestamps reaches the end of `text_display` with ≤ 30 % interpolated spans. Then starts a 4-clause unit, cancels after the first chunk, and records how many bytes arrive *after* `clear` (`clear_leak_measured`). Output: `traces/preflight_<utc>.jsonl`, `traces/preflight_<utc>.wav`.

### M3. Synthesis latency — `scripts/bench_latency.py`
50 fixture clauses on one persistent socket (warm) and 10 with a fresh socket each (cold). Per unit: chars, TTFB (request → first audio byte), total (request → `done`), audio ms, real-time factor. Reports p50 / p95 / max per condition. Measured at the server; **this is not audible latency** and is reported separately from A1. Output: `traces/latency_bench_<utc>.json` + `.jsonl`.

### M4. Number round-trip — `scripts/number_roundtrip.py`
40 golden strings (`tests/numbers.jsonl`) → normalizer (asserted equal to the committed spoken form) → Rime → WAV → Whisper → normalizer again → token comparison. A row passes when every number-bearing token (digits, currency, percent, ordinals, section letters) survives. Output: `traces/number_roundtrip_<utc>.json`, clips in `traces/numbers/`. This is a correctness floor, not a claim.

### M5. Word-map alignment — `tests/test_wordmap.py` (offline) + M2 (live)
Offline tests cover the "four b two" ↔ "4(b)(ii)" case, conservative mid-word boundaries, punctuation attachment, and merged/dropped Rime tokens. M2 reports the interpolated-span count on real timestamps.

## Results

> Filled from committed traces only. Every number below must name the trace file it came from. Blank means not yet run.

| Measure | Value | Trace |
|---|---|---|
| Catalog check date / speaker / model / lang | 2026-09-05T15:56:59Z / `bancroft` / `coda` / `eng` — confirmed present in the live catalog fetched from `https://users.rime.ai/data/voices/all-v2.json` | `traces/rime_catalog_20260905.json` |
| Preflight: audio ms, words, drift, interpolated spans | audio_ms 22080.0, words 65, last word end 22205.19 ms (trailing_ms −125.2 vs audio length); drift ratios measured separately per clause in "Timestamp fidelity" below (0.90 / 1.01 / 0.66) | `traces/preflight_20260905T163015Z.jsonl`, `traces/preflight_20260905T163015Z.timestamps.json` |
| Bytes (ms) arriving after `clear` | 1,112,576 bytes (≈23,178.7 ms of audio) still arrived after `clear`, measured over a 6 s wait — this is the leak the generation fence exists to drop, not a bug in the fence | `traces/preflight_20260905T163015Z.jsonl` → `clear_leak_measured` |
| TTFB cold p50 / p95 | 382.5 ms / 422.0 ms (n=10, mean 382.9 ms, max 422.0 ms) | `traces/latency_bench_20260909T160044Z.json` |
| TTFB warm p50 / p95 | 414.5 ms / 437.0 ms (n=10, mean 407.9 ms, max 437.0 ms) | `traces/latency_bench_20260909T160044Z.json` |
| Full-unit RTF cold p50 / p95 | 0.30 / 0.30 (mean 0.30, max 0.30) | `traces/latency_bench_20260909T160044Z.json` |
| Full-unit RTF warm p50 / p95 | 0.30 / 0.30 (mean 0.30, max 0.40) | `traces/latency_bench_20260909T160044Z.json` |
| Number round-trip pass rate + ASR model | Audio synthesized for all 46 fixture strings (`n: 46`), but `asr: null` — **no ASR/STT key is configured** (`STT_API_KEY` / `GROQ_API_KEY` / `OPENAI_API_KEY`), so 0 of 46 were actually scored (`n_scored: 0`, `n_pass: 0`). The number-normalization text pairs exist (display → spoken) but the round-trip claim — that Rime's audio, transcribed back, matches — is **not yet verified**. **Outstanding**: set one of the STT keys above and re-run `python scripts/number_roundtrip.py` | `traces/number_roundtrip_20260909T160227Z.json` |
| A1 audible stop p50 / p95 (far end) | **p50 2228.0 ms / p95 2657.3 ms** (n=8, samples: 2649.3, 1440.0, 838.7, 2188.0, 2228.0, 2648.0, 1100.0, 2657.3 ms) — loopback recording of the real Rime session, measured with `examples/policy-reader/acceptance/measure_far_end.py --skip-asr` | `traces/session_webf77b6df1.jsonl`, `traces/acceptance_far_end.json` |
| A2 delivered-text agreement | **Transcript captured, not scored.** `measure_far_end.py` (run on Astitva's own machine, open network) transcribed the same recording with faster-whisper `base.en`: 3115 chars, opening "I've gone through Carers Allowance, Eligibility, ..." — matches the session's actual `start_choice` line. **Outstanding**: `measure_a2` only transcribes; it does not compute the character-level agreement itself (its own docstring: "computed by the caller once the clip boundaries are aligned"). The trace's `unit_truncated` rows carry `char_end`/`of` offsets into the fixture text, not delivered text, so scoring this for real needs pulling the fixture text per `context_id`, slicing a clip per truncated unit from the WAV, and diffing each against its expected prefix — not built. No fabricated pass rate is reported. | `traces/session_web-f77b6df1.jsonl` (Astitva's machine; not yet copied into this repo's `traces/`) |
| A3 deictic resolution | **20/20** — offline scripted harness, 20 interruption points across the fixture (`examples/policy-reader/acceptance/run_script.py`, `TTS_PROVIDER=fake`). Every deictic question resolved against the last **heard** clause, never the last one sent. Zero failures. Live single-sample confirmation: interrupt at 8160 ms in `sec-5b-i`, boundary 126 chars (word 20, straddling `a`) from the live word map — `traces/demo_rime_20260906.jsonl` | `traces/acceptance_report.json` |
| A4 resume within one sentence | **20/20** — same 20-point harness run; every resume landed within one sentence of its cut point. Zero failures. Live single-sample confirmation: cut at char 126 (sentence 0 ends at 123), resumed as `sec-5b-i/resume#1` at `char_start=124`, the start of the sentence containing the cut — `traces/demo_rime_20260906.jsonl` | `traces/acceptance_report.json` |
| A5 no false deliveries | **20/20** — same harness run; no unit across any of the 20 points was marked heard without a matching `frames_played` ack. Zero failures | `traces/acceptance_report.json` |

## A1 measurement — two bugs found and fixed getting here

Getting a real A1 number surfaced two separate bugs, not just a recording
technique problem. Recorded as the QA sections above do, since both would
otherwise silently produce a wrong "no data" or a wrong number.

**1. The client never sent `audible_stop_ts`.** `delivery_layer/playback_protocol.py`'s
`FlushAck` has always carried an `audible_stop_ts` field, and `agent.py`'s
LiveKit path logs it correctly via `Ledger.log_audible_stop()`. But the actual
browser app (`examples/policy-reader/web`) builds its `flush_ack` messages by
hand in `session.ts`/`reducer.ts`, and those never included the field — every
recording produced a trace with `flush_ack` events but zero `audible_stop`
events, so A1 had nothing to measure against. Fixed: `AudioPlayer.audibleStopTs()`
(reads `AudioContext.currentTime`, the audio-hardware clock) is now threaded
through `buildInterrupt`/`buildPause`/`buildCut`/`buildOpen` and the flush
handler in `session.ts`, and `server.py`'s `flush_ack` handler now reads
`audible_stop_ts` off the incoming message and emits a matching `audible_stop`
event, mirroring the `agent.py` path. Confirmed on the recording used for this
row: 8 `flush_ack` events, 8 `audible_stop` events.

**2. `measure_far_end.py`'s `last_audible_ms` scanned to end of file.** It
returned whichever non-silent window it found last, however far past the
interrupt — on a session where reading continues after every interrupt (the
normal case), that is the end of the *next* speech burst, or the last one in
the whole recording, not the tail of the flushed audio. The first run against
this recording reported p50 179148 ms / p95 264769 ms — not a fluke, a
measurement bug. Fixed: it now stops at the first real silence gap
(`SILENCE_GAP_MS = 400`) after audio has started, bounded to a 5 s lookahead.
Re-run against the same recording: p50 2228.0 ms / p95 2657.3 ms, the number
in the Results table above.

Both fixes are in the committed scripts (`server.py`, `session.ts`,
`reducer.ts`, `measure_far_end.py`); nothing here is a one-off patch applied
only to produce this number.

## Timestamp fidelity

Rime's word timestamps are a nominal prediction. They arrive in the same
millisecond as the first audio byte, sit on a 180.53 ms grid, and drift against
the delivered audio by an amount that is not a function of clause length:
`sec-4b-vii` (354 chars) ratio 0.90, `sec-7b-ii` (54) ratio 1.01, `sec-5b-iv`
(62) ratio 0.66 — measured by `scripts/preflight_rime.py`.

Everything in this document that says "heard", "boundary" or "delivered"
therefore derives from **client-acked rendered frames compared against a
byte-derived audio length**, never from timestamps. Word maps are stretched onto
the measured audio at `Done` and their spans marked `estimated`; the
highlight claim is **clause-level**, and the word-level figures in the results
table are reported with their interpolated-span count so the granularity is not
overstated.

## Chunk integrity

Rime `/ws3` splits its 1024-byte PCM blocks at arbitrary byte offsets. In
`traces/preflight_20260905T163015Z.jsonl` the splits 829+195, 655+369 and
1006+18 appear; 98 of 1181 chunks were an odd number of bytes, ending halfway
through an s16le sample. The client decoded each chunk on its own, threw on the
odd ones, and the async message handler swallowed the error: both halves of
the split block were lost. Heard as ~20 ms holes in the speech, and measured as
`rendered_ms` plateauing short of `audio_ms` on every clause.

Fix: the adapter carries the odd byte into the next chunk (`chunk_realigned`
per unit, with the odd-chunk count) and the client does the same; the server
marks a unit heard only when the client's enqueued frame count equals the
bytes it sent, and logs `frame_count_mismatch` otherwise. There is no plateau
tolerance: the only slack is one 128-frame render quantum.

Live session `traces/session_web-29e08c00.jsonl` — listener route in a
browser, document `carers_allowance`, server serving `index-Daz9M1lq.js`.
`deficit` is `(audio_ms - final rendered_ms)` in frames at 24 kHz; `odd` is the
number of odd-length chunks the carry realigned in that unit.

| context | audio_ms | final rendered_ms | deficit (frames) | odd | frame_count_mismatch | heard |
|---|---:|---:|---:|---:|---:|---|
| `sec-2-p1#t1` | 10240.0 | 2730.7 | 180223 | 48 | 0 | no — listener paused at 2.7 s |
| `sec-3-p2#t3` | 5120.0 | 1621.3 | 83969 | 18 | 0 | no — listener paused at 1.6 s |
| `sec-3-p3#t4` | 6720.0 | 6720.0 | 0 | 16 | 0 | yes |
| `sec-3-p5#t6` | 7120.0 | 7120.0 | 0 | 20 | 0 | yes |
| `sec-3-p6#t7` | 8880.0 | 8880.0 | 0 | 34 | 0 | yes |
| `sec-3-p7#t8` | 8160.0 | 8160.0 | 0 | 30 | 0 | yes |

`render_ack_timeout`: 0. `frame_count_mismatch`: 0. `chunk_realigned`: 6 of 6
units — every clause in the session contained odd-length chunks (16 to 48), so
before the carry every clause lost audio.

On every unit played to completion the final `rendered_ms` equals `audio_ms`
exactly: a deficit of 0 frames, not merely within one quantum. The two unheard
units were paused by the listener mid-clause; they are correctly not heard, and
because a paused unit no longer emits `unit_ended`, they produce no mismatch —
`frame_count_mismatch` now means dropped audio and nothing else.

Two caveats on this run. It was played clause by clause (a pause and a play at
each boundary; 8 `cancel_issued`), so `unit_ended`, the client's completion
signal that needs a successor unit buffered behind the finished one, was not
exercised live; it is covered by `tests/test_server_heard.py` and
`web/src/store/session.test.ts`. And the same trace shows why the reader
stopped at each clause: the flow-control backlog counted the unplayed remainder
of paused units, and the pause path did not rewind the read cursor
(`sec-3-p4` was skipped after a pause at 0.9 s). Both are recorded here as found
and addressed in the commit that follows this evidence: flow control now counts
only units still deliverable, and pause attributes its boundary exactly as an
interrupt does. That commit is verified by `tests/test_server_pause.py` and
the reducer tests, not yet by a further live session; a continuous run that
reaches `document_finished` without a pause is the outstanding check.

## Grounding correctness (not retrieval quality)

Every answer carries the branch that produced it: `retrieval_path` on the
`answer_grounded` event and on `question_resolved`, one of `deictic`,
`definition`, `section_ref`, `bm25`, `none`, in that resolution order. The
claim is narrow: the scripted questions for the hero fixture resolve to the
expected clause by the expected branch. Nothing is claimed about questions
outside the script.

```bash
python scripts/check_grounding.py --out traces/grounding_check_policy.json
```

Output, one row per question, then a summary by branch:

```
     question                              expected                  actual
HIT  what does that mean                   deictic/sec-4b-ii         deictic/sec-4b-ii (deictic)
HIT  what does bodily injury mean          definition/sec-3a-iii     definition/sec-3a-iii (in_scope)
MISS <question>                            bm25/<expected>           bm25/<actual> (<kind>)

<hits> of <n> hit  deictic: h/n  definition: h/n  section_ref: h/n  bm25: h/n  none: h/n
```

Exit code 0 only when every row is a HIT. The JSON written by `--out` holds
the same rows plus the listener position each question was asked from
(`last_heard`, `cursor`). Numbers go in the table below only from a committed
`traces/grounding_check_policy.json`.

| Fixture | Questions | Hits | deictic | definition | section_ref | bm25 | none | Trace |
|---|---:|---:|---:|---:|---:|---:|---:|---|
| `policy.json` | 20 | | | | | | | `traces/grounding_check_policy.json` |

## Navigator and jump (scope)

The navigator's generated fields are produced at build time and marked in the
fixture; nothing in the judged runtime path calls a model. What is verified by
test (`tests/test_server_jump.py`, `tests/test_enrich.py`): a jump issued while
unit N plays and N+1 is in flight truncates N at the client boundary, skips
N+1 (`unit_skipped`, reason jump), writes one `jump` event with a `turn_id`,
speaks the cue unit, then the target; "go back to where I was" returns; a
topic chip asks "now or overview first" and both answers reach the target;
"read me every exclusion" returns cited clauses without touching BM25; a
generated question whose clause id does not resolve is dropped; advisory
language is rejected by the guard and falls back to the mechanical form.
Scope: the generality claim rests on two fixtures (hero and two-wheeler) and
the interruption drift script at 15 points; retrieval quality is still not
claimed.

## Limitations of the evidence

- Latency benches run from a single region and network; they characterise our deployment, not Rime globally.
- Whisper is itself lossy; a round-trip failure is investigated by listening to the clip before being attributed to Rime.
- The `clear` leak measurement depends on how much text was queued; we report the queued length alongside the bytes.
- Word-level boundaries depend on Rime's timestamps. Spans the aligner could not match are interpolated and counted; if that count is non-trivial in a run, the boundary claim for that run is clause-level, not word-level.
- `modelId` is asserted from configuration and the catalog check, not from the audio stream, because the stream does not identify the model.
- A3/A4/A5's 20/20 result is from the offline scripted harness on `TTS_PROVIDER=fake`, not live Rime — it verifies the ledger/fence/position-manager logic deterministically and is reproducible on demand, but is not a substitute for A1/A2 (audible stop, delivered-text agreement), which specifically require the real Rime path and a recording of actual speaker output.
- A1 is a single recorded session (n=8 interruption points), not the full 20-point script; it establishes the measurement pipeline works end-to-end and gives a real, if small-sample, p50/p95. A2's transcript was captured from the same session but not scored against the fixture text — the character-level comparison the acceptance test calls for was never wired up in `measure_far_end.py`, so no A2 pass rate is claimed.

## Narration gap (no radio silence while a document is processed)

**Claim.** While a tab has the voice and a document is ingested and enriched,
the longest silence between spoken companion lines is bounded; the target is
a 95th percentile under 15 s. The lines are templates on real stage events, one
engagement question, an acknowledgement or a plan line, and at most five
fillers per ingest; none carries a fact from the document.

**Metric.** `narration_gap_ms{max, count, gaps}` is written to the session
trace once per ingest by `Narrator.finish()` (`companion.py`): `gaps` are the
milliseconds between the upload's start, each `companion_spoken` line's start,
and the end of the ingest; `max` is the longest; `count` the lines spoken.
The lines themselves are `companion_spoken{source, origin, text}` records, so
a gap can be read back to what was and was not said.

**Procedure.** Claim the voice (play, or an interrupt on a fresh session),
upload a document from that tab, and read the trace:

    python - <<'EOF'
    import json, sys
    rows = [json.loads(l) for l in open("traces/session_<id>.jsonl")]
    for r in rows:
        if r["type"] == "narration_gap_ms": print(r["max"], r["count"], r["gaps"])
    EOF

Numbers quoted in this file come only from committed traces under `traces/`.
The tests `tests/test_server_navigator.py::NavigatorCase::test_slow_stages_are_bridged_without_a_reply`
and `..._a_reply_during_slow_stages_...` hold stage events fifteen seconds
apart and assert `max < 15000` with the fake voice.

