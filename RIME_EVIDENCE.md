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
| Catalog check date / speaker / model / lang | | `traces/rime_catalog_check.json` |
| Preflight: audio ms, words, drift, interpolated spans | | `traces/preflight_*.jsonl` |
| Bytes (ms) arriving after `clear` | | `traces/preflight_*.jsonl` → `clear_leak_measured` |
| TTFB warm p50 / p95 | | `traces/latency_bench_*.json` |
| TTFB cold p50 / p95 | | `traces/latency_bench_*.json` |
| Full-unit RTF warm p50 / p95 | | `traces/latency_bench_*.json` |
| Number round-trip pass rate + ASR model | | `traces/number_roundtrip_*.json` |
| A1 audible stop p50 / p95 (far end) | | acceptance harness (Person B) |
| A2 delivered-text agreement | | acceptance harness |
| A3 deictic resolution | interrupt at 8160 ms in `sec-5b-i`; the deictic question resolved against the last **heard** clause, boundary 126 chars (word 20, straddling `a`) from the live word map | `traces/demo_rime_20260906.jsonl` |
| A4 resume within one sentence | cut at char 126, sentence 0 ends at 123; resumed as `sec-5b-i/resume#1` with `char_start=124`, the start of the sentence containing the cut. Read cursor advanced to the next unit, not a replay | `traces/demo_rime_20260906.jsonl` |
| A5 no false deliveries | | acceptance harness |

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

## Limitations of the evidence

- Latency benches run from a single region and network; they characterise our deployment, not Rime globally.
- Whisper is itself lossy; a round-trip failure is investigated by listening to the clip before being attributed to Rime.
- The `clear` leak measurement depends on how much text was queued; we report the queued length alongside the bytes.
- Word-level boundaries depend on Rime's timestamps. Spans the aligner could not match are interpolated and counted; if that count is non-trivial in a run, the boundary claim for that run is clause-level, not word-level.
- `modelId` is asserted from configuration and the catalog check, not from the audio stream, because the stream does not identify the model.
