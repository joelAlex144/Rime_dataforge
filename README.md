# Delivery-aware position layer

**What this solves:** voice agents that read long documents aloud break the moment someone interrupts them. The server doesn't actually know where playback stopped — only the client's speaker does — so a "smart" agent ends up either replaying whole paragraphs, answering from the wrong clause, or losing track of what the listener actually heard. This project fixes that at the transport layer, not by making the LLM smarter.

## The scenario

A voice agent reads a long insurance policy or loan agreement aloud to someone consuming it by ear — on a call, while driving, hands or eyes occupied, or because a 40-page document in print isn't practical for them. They interrupt mid-clause to ask a question. The agent must answer against the **last clause the listener actually heard** — never the last one the server merely *sent* — and resume within one sentence of wherever they were cut off.

Text doesn't have this problem: it's delivered atomically, sent equals received. Speech is delivered over time, so a listener can be cut off mid-word, and only the client actually rendering audio knows where playback really stopped. This layer owns that boundary honestly instead of trusting the server's send position as if it were the truth.

## What's being judged

**The product is `delivery_layer/`** — the ledger, the fence, the position manager, the resume logic. `examples/policy-reader/` is the demo environment that makes the failure visible; it is not the product.

Two failure modes, claimed in this order:

- **Primary — interruption and recovery.** Stop synthesis and playback promptly, fence out any stale audio still arriving after a cancel, and keep state consistent with what the client actually rendered — never with what the server assumed.
- **Supporting — evaluation and observability.** Every session emits an append-only, per-clause record of what was actually heard, truncated, or skipped — the artifact that makes "delivery-aware" a checkable claim, not a description.

## Start here: the evidence

**→ [`RIME_EVIDENCE.md`](./RIME_EVIDENCE.md)** is the whole case. Every number in it traces to a committed file under `traces/` — no trace, no number. It has the hard voice claim, the acceptance test (A1–A5) and procedure, real measured results, two real bugs found and fixed while getting those numbers (documented, not swept under the rug), and an honest account of what's still outstanding.

If you only read one file to evaluate this, read that one.

## Run it in 60 seconds (no keys needed)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python -m pytest                             # offline, no key needed

# offline demo, fake TTS provider, still exercises the full interruption path
TTS_PROVIDER=fake python examples/policy-reader/read_demo.py --cut-ms 2500 -q "what does that mean"
```

For the real Rime voice and the full web demo (two terminals, needs `RIME_API_KEY`), see [`docs/TECHNICAL.md`](./docs/TECHNICAL.md#setup) and the [Web demo](./docs/TECHNICAL.md#web-demo) section there.

## Rime integration, at a glance

| Field | Value |
|---|---|
| Model ID | `coda` |
| Speaker | `bancroft` / `eyre` — verified against the **live catalog** at run time (`scripts/fetch_voices.py`), never a stale hardcoded list |
| Language | `en` |
| Endpoint | `wss://users-ws.rime.ai/ws3` |
| Audio format | `pcm`, mono 16-bit, 24000 Hz |

Rime is the primary, judged spoken output. `TTS_PROVIDER=fake` is a disclosed offline fallback for development only — every session logs which provider actually ran, so the two can never be confused in a trace. Full table with enforcement points: [`docs/TECHNICAL.md#rime-integration`](./docs/TECHNICAL.md#rime-integration).

## Repository map

```
delivery_layer/          the product — ledger, fence, position manager, resume logic
examples/policy-reader/  the demo environment that makes the failure visible
scripts/                 fetch_voices, preflight_rime, bench_latency, ingest, number_roundtrip
tests/                   offline, no key needed
traces/                  committed evidence — every number in RIME_EVIDENCE.md traces here
RIME_EVIDENCE.md         the hard voice claim, acceptance test, results, limitations — start here
docs/TECHNICAL.md        full implementation reference: setup, ingestion, navigator, web demo, design decisions
```

## Known limitations (short version)

- English only, browser/app transport only — no telephony, no code-switching.
- Fixed, pre-chunked fixtures; retrieval is BM25 with a synonym table, not a RAG project.
- Word-level highlight timing is Rime's estimate, not a measurement — clause-level is the honest granularity claim (measured drift documented in `RIME_EVIDENCE.md`).
- A2 (delivered-text ASR agreement) is transcript-only so far, not yet scored — see `RIME_EVIDENCE.md` for exactly what's missing.

Full list, with the reasoning behind each: [`docs/TECHNICAL.md#known-limitations`](./docs/TECHNICAL.md#known-limitations).
