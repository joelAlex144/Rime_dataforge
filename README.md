# Delivery-aware position layer

A voice agent that reads a long insurance policy aloud, takes questions mid-clause, answers them against the **last clause the listener actually heard**, and resumes within one sentence of where they were cut off — with a per-session record of which clauses were heard.

**The product is the layer. The policy reader in `examples/policy-reader/` is the environment that makes the failure obvious.**

Text is delivered atomically: sent equals received. Speech is delivered over time, so a message can be half-received, and only the client knows where it stopped. This layer owns that boundary.

## Repository layout

```
delivery_layer/            the product
  events.py                append-only event log (the evidence artifact)
  normalize.py             display text -> spoken text, with a char-span map
  wordmap.py               Rime word timestamps <-> display characters
  resume.py                sentence-boundary resume point + re-entry cue
  tts/base.py              TTSProvider contract (AudioChunk | Timestamps | Done, cancel())
  tts/rime.py              Rime /ws3 adapter: persistent socket, contextId fencing
  tts/fake.py              offline provider with the same contract (disclosed fallback)
examples/policy-reader/    the demo environment
  fixtures/policy.json     synthetic homeowners policy, 213 clauses, stable ids
  fixtures/build_fixture.py
  grounding.py             BM25 + spoiler gate + deictic resolution + LLM prompt
  read_demo.py             offline end-to-end of this slice
scripts/
  fetch_voices.py          live catalog check — fails if speaker/model/lang absent
  preflight_rime.py        exact shipped path: one clause, asserts PCM + timestamps
  bench_latency.py         TTFB / full-unit synth, cold vs warm, 50 units
  number_roundtrip.py      golden set -> Rime -> Whisper -> compare
tests/                     pytest / unittest; tests/numbers.jsonl is the 40-string golden set
traces/                    committed evidence (catalog snapshot, preflight, bench, round-trip)
RIME_EVIDENCE.md           claim, acceptance test, procedure, results, limitations
```

Owned by Person B (not in this slice): client AudioWorklet + playback acks, turn guard, delivery ledger, scheduler, position manager, LiveKit agent, acceptance harness.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env            # fill RIME_API_KEY and RIME_SPEAKER; never commit .env
set -a; source .env; set +a

python scripts/fetch_voices.py --list        # pick a coda / en speaker from the LIVE catalog
python scripts/fetch_voices.py               # exit 0 == configured combination exists today
python scripts/preflight_rime.py --clear     # exact shipped path; writes traces/preflight_*.jsonl + .wav
python -m pytest                             # offline; no key needed

TTS_PROVIDER=fake python examples/policy-reader/read_demo.py --cut-ms 2500 -q "what does that mean"
TTS_PROVIDER=rime python examples/policy-reader/read_demo.py --cut-ms 2500 -q "what does that mean"
```

## Rime integration

| Field | Value | Where enforced |
|---|---|---|
| Model ID | `coda` | `RimeConfig.model_id`; sent explicitly on every connection |
| Speaker | `${RIME_SPEAKER}` — `bancroft` or `eyre` at time of writing | `scripts/fetch_voices.py` fails if absent from the live catalog; snapshot in `traces/rime_catalog_<date>.json` |
| Language | `en` | `RIME_LANG`; required for word timestamps |
| Endpoint | `wss://users-ws.rime.ai/ws3` (us-west-2) | `RIME_WS_URL` |
| Audio format | `pcm` — raw signed 16-bit LE mono, 24000 Hz | `RIME_AUDIO_FORMAT`, `RIME_SAMPLING_RATE`; preflight rejects a WAV header |
| Transport | Rime → server over WSS (one persistent socket, `flush` per unit); server → client over LiveKit WebRTC | `tts/rime.py`, LiveKit agent |
| Segmentation | `segment=bySentence`, `speedAlpha=1.0` | `RimeConfig` |

The active provider and all of the above are logged as a `provider_active` event at connect time. `modelId` cannot be inferred from the audio stream — Rime silently serves Mist v3 if the parameter is missing or misspelled — so the catalog check plus the logged config are the guard.

**Fallback (disclosed):** `TTS_PROVIDER=fake` swaps in `tts/fake.py` for offline development. It emits `provider_active` with `provider: fake`; a session that ran on it cannot be mistaken for a Rime run in the trace. Rime is the default and the judged path.

## How the layer works (this slice)

1. **Normalizer** rewrites currency, percentages, dates, times, section references, and policy numbers into spoken form *and* returns a span map so every spoken token traces back to display characters. 40-string golden set in `tests/numbers.jsonl`.
2. **Rime adapter** keeps one `/ws3` socket open, sends `{text, contextId}` + `flush` per clause, and demultiplexes replies by `contextId`. `cancel()` bumps a generation counter, ends every in-flight iterator, then sends `clear`. Rime's `clear` does not stop audio already synthesised, so anything arriving for a stale context is dropped and logged as `result_fenced` with its byte count — the leak the fence prevents is measured, not assumed.
3. **Word map** aligns Rime's word timestamps to the span map (token-level `SequenceMatcher`, tolerant of merged/dropped tokens; unmatched spans are interpolated and flagged). `WordMap.offset_at(rendered_ms)` is the delivery boundary in display characters; a half-played word does not count as heard.
4. **Grounding** answers only from fixture text. Deictic questions ("what does that mean") resolve to the last *heard* clause supplied by the ledger. Retrieval scope is capped at the read cursor; a better hit further down produces an offer to jump, never a read-ahead. No interpretation: the model cites the section and redirects to the insurer otherwise.
5. **Resume** restarts at the start of the sentence containing the boundary, with a short cue.

## Design decisions (already made)

- Unit granularity is clause-level; **word-level offsets are used because `/ws3` returns word timestamps for English at no extra cost**. Interpolated spans are flagged in the word map and counted in preflight.
- The delivered set is client-acknowledged, never server-estimated.
- Resume at the sentence boundary before the cut with a re-entry cue; never replay the whole clause.
- Truncated text enters LLM context as delivered-verbatim plus `[interrupted]`.
- No legal or clinical interpretation.
- Synthetic data only. `fixtures/policy.json` is an invented policy for an invented insurer.

## Known limitations

- English only. No code-switching.
- Browser/app transport only. No telephony, no adverse-audio claims.
- Fixed, pre-chunked fixture; retrieval is BM25 with a small synonym table. Not a RAG project.
- `clear` on `/ws3` does not cancel in-flight synthesis. Audible stop depends on the client flushing its playback queue; the adapter only guarantees stale audio is never forwarded.
- Timestamp clock behaviour (`per_context` vs `cumulative`) is a probe result, set in `.env`; preflight fails loudly if it looks wrong.
- Synthesis latency in `traces/latency_bench_*` is measured at the server, not at the listener. Audible-stop latency is a separate measurement in the acceptance harness.
- Rime's `/textnorm` is compared against our rules for information only; the shipped path uses our normalizer so the golden set is reproducible offline.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Rime socket drops mid-unit | `provider_disconnected` logged; in-flight iterators receive `TTSError`; agent reconnects and resumes from the last acknowledged boundary |
| Late audio after cancel | dropped, `result_fenced` with byte count |
| Speaker not on live catalog | `fetch_voices.py` exits 1 — submission blocker |
| Question about unread clause | offer to jump; clause not read |
| Question not answerable from text | says so, redirects to insurer |
| LLM unavailable | extractive answer: cite and re-read the clause |
