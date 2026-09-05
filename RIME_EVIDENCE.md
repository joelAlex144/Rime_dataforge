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
| A3 deictic resolution 20/20 | | acceptance harness |
| A4 resume within one sentence 20/20 | | acceptance harness |
| A5 no false deliveries | | acceptance harness |

## Limitations of the evidence

- Latency benches run from a single region and network; they characterise our deployment, not Rime globally.
- Whisper is itself lossy; a round-trip failure is investigated by listening to the clip before being attributed to Rime.
- The `clear` leak measurement depends on how much text was queued; we report the queued length alongside the bytes.
- Word-level boundaries depend on Rime's timestamps. Spans the aligner could not match are interpolated and counted; if that count is non-trivial in a run, the boundary claim for that run is clause-level, not word-level.
- `modelId` is asserted from configuration and the catalog check, not from the audio stream, because the stream does not identify the model.
