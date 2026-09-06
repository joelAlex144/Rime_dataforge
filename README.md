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
  fixtures/index.json      document registry — the ONLY source of openable documents
  fixtures/README.md       what each committed fixture is, and its extraction problems
  grounding.py             BM25 + spoiler gate + deictic resolution + eligibility refusal
  library.py               document registry + per-document Session (cursor, ledger, history)
  segment.py               shared segmentation primitives (sentence_spans copy, markers)
  read_demo.py             offline end-to-end of this slice
  chat_demo.py             text REPL over the library (docs / open / read / stop / ask / resume)
scripts/
  ingest.py                build-time document -> fixture (PDF/DOCX/HTML/URL/txt)
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
pip install -r requirements-build.txt   # docling, pinned: the structure pass behind ingest and upload
docling-tools models download            # one-time, ~500 MB to ~/.cache/docling; a judge reproducing live needs it
cp .env.example .env            # fill RIME_API_KEY and RIME_SPEAKER; never commit .env
set -a; source .env; set +a

python scripts/fetch_voices.py --list        # pick a coda / en speaker from the LIVE catalog
python scripts/fetch_voices.py               # exit 0 == configured combination exists today
python scripts/preflight_rime.py --clear     # exact shipped path; writes traces/preflight_*.jsonl + .wav
python -m pytest                             # offline; no key needed

TTS_PROVIDER=fake python examples/policy-reader/read_demo.py --cut-ms 2500 -q "what does that mean"
TTS_PROVIDER=rime python examples/policy-reader/read_demo.py --cut-ms 2500 -q "what does that mean"
```

### Third-party libraries

| Library | Where | Note |
|---|---|---|
| `websockets`, `requests` | runtime | Rime `/ws3`, catalog check |
| `aiohttp` | web demo | one port for HTTP + `/ws/audio` |
| `pypdf`, `python-docx`, `beautifulsoup4` | build time | line-based extractors in `scripts/ingest.py` |
| `docling` | build time only | structure pass; runs locally, no API, pinned in `requirements-build.txt`; models downloaded once with `docling-tools models download` |

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

## Bring your own document (build time only)

The reader is not tied to the insurance policy. `scripts/ingest.py` turns a PDF,
DOCX, HTML file, URL, or text file into a fixture in exactly the schema above,
and `examples/policy-reader/chat_demo.py` is a text REPL that exercises the
grounding layer against any fixture — no audio, no Rime, no LiveKit.

```bash
python scripts/ingest.py <path-or-url> --dry-run                    # read the clause list
python scripts/ingest.py <path-or-url> --out examples/policy-reader/fixtures/<name>.json --review
python examples/policy-reader/chat_demo.py --fixture fixtures/<name>.json
```

Three rules, all load-bearing:

- **Ingestion is build time only.** `ingest.py` may fetch a URL *when a developer
  runs it*; its output is a committed JSON file. There is no runtime upload and
  no runtime URL fetching anywhere in the agent path — the delivery layer only
  reads fixtures that are already in version control.
- **The review step is mandatory.** `--review` prints every clause and waits.
  A fixture is not committed without a human reading that list, because the
  segmenter fails silently: a swallowed heading, a dropped list introducer, or a
  site's feedback widget read aloud as policy text all pass validation. Both
  splitter bugs fixed while ingesting the second fixture were found this way,
  not by a test.
- **The hero document does not change.** `fixtures/policy.json` and
  `fixtures/build_fixture.py` are frozen; new fixtures sit beside them.
  `examples/policy-reader/segment.py` holds a deliberate copy of
  `build_fixture.sentence_spans`, and `tests/test_ingest.py` asserts the two stay
  identical rather than importing or editing the frozen file.

Segmentation honours a document's own numbering when it has one
(`Section 4 > (b) > (ii)` → `sec-4b-ii`, `4.2.1` → `sec-4-2-1`) and falls back to
headings-as-sections with `sec-<n>-p<k>` ids when it does not. Clauses may carry
two optional fields the hero fixture does not use: `kind`
(`clause` | `table_row` | `heading`) and `path` (the human numbering path, e.g.
`4(b)(ii)`). Nothing else in the schema changed, so `grounding.py`, `wordmap.py`,
`resume.py` and `read_demo.py` read both fixtures unmodified.

### Structure pass and the one ingestion function

`scripts/ingest.py` exposes `ingest_document()`, and that one function is what
both the build-time CLI and the runtime upload (`POST /documents`) run, so the
same PDF gives the same fixture bytes on every machine and `doc_id` is a
content hash of the source. Every run goes through every stage --
`extract -> structure -> segment -> normalize -> pii_scan -> validate ->
write` -- and writes an ingest report next to the fixture as
`<doc_id>.ingest_report.json`. Nothing in the report blocks a document. For a
PDF or a `.docx` the structure pass is Docling's layout model, OCR off (there
is `--ocr` for a scanned one), run locally with no API; the lossless
DoclingDocument is saved beside the fixture as `<doc_id>.docling.json` and is
the reproducibility artifact. Text, Markdown and HTML go through the line-based
extractors and then the same downstream mapping. Docling loads its models
once; the server pre-builds the converter at start so the first upload is not
cold. Every clause gets a `kind`:

| `kind` | From | Spoken? |
|---|---|---|
| `heading` | `section_header` | yes, as a signpost: `"{heading}. {n} items."` (n = direct body children); the clause-id anchor for everything under it |
| `body` | `text`, `paragraph`, `list_item`, `caption`, `footnote` | yes; list markers stay in the display text |
| `definition` | `body` under a Definitions / Interpretation heading, and definitions-table rows | yes; also indexed in the fixture's `terms` block |
| `table_stub` | `table` | yes: `"There is a table here: {caption or header row}. Ask me for any row."` |
| `table_row` | one per table row | on request only (`spoken_on_request`); linear playback skips it and logs `unit_skipped`; a question can land on it |
| `boilerplate` | `page_header`, `page_footer`, any unmapped label, plus the regex pass | never sent; keeps its place in reading order; logged as `unit_skipped` |

The regex pass demotes what the layout model leaves in body text: page
numbers, standalone UIN codes, registered-office / CIN / IRDAI-registration
lines, and any line recurring on 30 % or more of pages; `<<placeholder>>`
fields are stripped and the clause flagged `has_placeholder`. A clause over
the 1,000-character hard limit is split at sentence boundaries with the same
splitter the resume path uses and suffixed `-s1`, `-s2`; every split is
logged. The fixture also gains a `map` block (ordered top-level headings with
child counts), which the reader speaks once at session start as a plain
count, and `terms` (normalised term -> definition clause id).

### Personal data versus institutional contact details

`scripts/ingest.py` scans the extracted text before it segments anything, and
splits what it finds:

- **Personal** refuses the document (exit 2): an email at a private domain
  (gmail, yahoo, outlook, hotmail, rediffmail, proton), an email or phone
  beside a person's name, a 10-digit Indian mobile that is not next to
  helpline wording, a street address beside a person's name, and a name beside
  an account or policy number.
- **Institutional** is kept and warned: a role-based mailbox (`info@`,
  `care@`, `grievance@`, `complaints@`, `claims@`, `nodal@`, `bima…@`,
  `rgicl…@` and so on, or `x.care@` / `x.support@`), a regulator, bank or
  government domain (`irdai.gov.in`, `*.gov.in`, `*.nic.in`, `cioins.co.in`,
  `rbi.org.in`, `sebi.gov.in`, `npci.org.in`), the insurer's own domain when
  its name is on the cover, any address that appears three or more times
  (footer boilerplate), a toll-free 1800/1860 number, and any number within
  40 characters of helpline / toll free / customer care / grievance / call
  centre / contact us. Indian policy wordings carry these by regulation.

Institutional hits are printed as `warning: institutional contact detail kept`
and written, redacted, into the fixture's `source.institutional_contacts` so a
reviewer sees that the document has a helpline and a grievance mailbox without
the fixture repeating them. `--pii-report PATH` writes the same split as JSON.

The scan never blocks: personal-looking identifiers go into the ingest report
with the clause id they were found in, and the developer page shows them as
the review trail. Data policy: only public product wordings are ingested,
which is enforced by selection, not by the scanner.

The Reliance General *Arogya Sanjeevani* wording that the dev upload refused
(hits `rgic…@` and `bima…@`) now passes: 2 insurer addresses, 17 ombudsman
addresses and 3 toll-free numbers are kept as institutional, nothing personal.

### Document library

**Selecting a document at runtime is supported. Ingesting one is not.**
`examples/policy-reader/fixtures/index.json` is the only source of available
documents: `library.py` opens what the registry names and nothing else, so
"choose a document" can never widen into "load arbitrary text at runtime".
`scripts/ingest.py` appends or updates a registry entry when it writes a fixture
— `{name, title, path, source, clause_count, ingested_at}`, `name` defaulting to
the output filename stem and required to be unique. A fixture that is written but
not registered is invisible to the reader, which is deliberate: registration is
the moment a document becomes selectable.

```
docs               list the library
open <name>        switch, keeping your place in the document you leave
```

Each document owns a `Session` — read cursor, last heard unit, delivery
boundary, ledger, question history. Switching emits `document_opened`,
`position_saved` for the document being left, and `position_restored` for the one
being entered, then restores the incoming session untouched: come back to a
document and the cursor, last-heard clause and boundary are exactly as you left
them, and a deictic question still resolves to the clause you were cut off in.
Activity in one document cannot alter another's ledger. `Grounding` instances are
built lazily and cached per document, so a six-document library does not pay to
index five documents nobody opened. `Library.save()` / `.load()` persist sessions
only — never fixtures, which are large, committed and immutable.

Retrieval stays per-document by design. A question asked while B is open is
answered from B, and `not_found` is the correct outcome for something that only
appears in A; there is no cross-document retrieval and there should not be, since
answering from a document the listener is not in is a position leak of exactly
the kind the spoiler gate exists to prevent. `--fixture` still works and simply
builds a library of one.

**The agent refuses eligibility determinations.** "Am I eligible", "do I
qualify", "can I claim", "will they pay" and similar second-person outcome asks
are tagged `eligibility` by `Grounding.resolve()`. The agent reads the criteria
and cites the clause, then says it cannot apply them to the listener. That answer
is built deterministically and never goes through the LLM, so no sampling
accident can turn it into a yes or a no; the same rule is in `SYSTEM_PROMPT` as
defence in depth. The spoiler gate still outranks it — an eligibility ask never
pulls an unread clause forward.

## Web demo

Two React routes over one server and one event stream: `/` is the listener,
`/dev` is developer diagnostics. They share a single `useReducer` store, so the
two pages cannot disagree about what was heard -- `/dev` is a rendering of the
event log that `/` is driven by, not a second measurement path.

```bash
pip install -r requirements.txt
cd examples/policy-reader/web && npm install && cd -

# terminal 1 -- offline, no key, no network
TTS_PROVIDER=fake python examples/policy-reader/server.py

# terminal 2
cd examples/policy-reader/web && npm run dev     # http://localhost:5173  and  /dev
```

`npm run dev` proxies `/api` and `/ws` to the server on port 8080. The browser
talks only to our server: no provider key is ever sent to the client, and none
appears in the bundle. Use the real voice with `python examples/policy-reader/server.py`
after `set -a; source .env; set +a`.

**Upload on both routes.** `POST /documents` (multipart PDF; `.docx`, `.txt`,
`.md`, `.html` or a `{"url": ...}` body take the same path) runs
`ingest_document()` in a worker thread and streams the seven stages as
server-sent events `{stage, status, elapsed_ms}`; the last event carries the
library entry `{doc_id, title, reviewed, readable, clause_count}`. The entry is
appended to `index.json` at once with `reviewed: false`, and the reader can
start on it. The listener page renders only a progress bar keyed to the
stages, the elapsed time and the title once it lands: no stage names, no
counts, no scan findings, no accept step. The developer page renders the stage
list, the report in full (`GET /documents/<doc_id>/report`) and an **Accept**
button (`POST /documents/<doc_id>/accept`) that sets `reviewed: true`; the
badge on the listener page goes from "unreviewed" to nothing. Uploading the
same bytes twice returns the existing entry. A 25 MB cap and an unsupported
type are the only HTTP errors. A scanned or empty PDF still enters the
library, with `readable: false`, and the picker says "No readable text found"
instead of offering play.

**Questions.** Enter in the question box while the voice is reading is a
Stop-and-ask: the client sends `flush_ack`, `interrupt`, `ask`, so the
boundary is the playhead and the reader is stopped before the question is
resolved. The answer is synthesised through the same provider as a unit of its
own (`answer#t<n>`, `unit_started` with `kind: "answer"`), so it is heard on
the client's acks and interruptible like a clause. Once the client reports the
answer's last frame, reading resumes after 600 ms at the sentence containing
the cut, unless the answer was *beyond cursor* or *not found*, which wait for
Jump there / Keep going / play. Deictic questions resolve against the clause
the flush ack named, never the last clause synthesised under lookahead. With
`LLM_API_KEY` set, in-scope answers go through the model named by
`LLM_PROVIDER` / `LLM_MODEL` (key server-side only; `/api/status` shows which)
and the trace records `answer_source`; without it, or if the call fails, the
answer is extractive. Eligibility questions never go through the model.

**What `--dev` enables.** `python examples/policy-reader/server.py --dev` turns on
provider swapping (`/api/dev/provider`) and the developer page's controls.
Upload (`POST /documents`) is not behind it: it is a first-class path on both
routes. Never enable `--dev` for the judged flow.

**The reviewed flag.** There is no quarantine directory. A library entry
carries `reviewed`, set by a person (the Accept button, or by hand in
`index.json`), and `readable`, set by the structure pass. The five hand-checked
IRDAI/lender wordings are `reviewed: true`; anything uploaded is `false` until
accepted.

**Trace replay, for judges with no key.** `/dev` lists `traces/*.jsonl` and
replays a committed one at 20x into the same event stream, emitting
`replay_start` and `replay_end` around it. The status strip, context table,
metrics and event list are all driven from that stream, so the evidence can be
inspected with no Rime credentials and no audio. Playback on `/` is disabled
while a replay is running.

**Screenshots must never include `.env` or a key.** Capture the browser window,
not a terminal that has sourced the environment. `/dev` shows the provider
descriptor -- model, speaker, language, format, sample rate, endpoint -- and
never the key.

What the listener sees is deliberately narrow: no clause ids, no milliseconds,
no provider name, no turn ids. Heard text is ink, unheard is grey, and the
delivery boundary is a rule at the exact character the server derived from the
client's audio clock. Text after the boundary stays grey even though the server
has already sent that audio -- that gap is the whole point of the layer.

### Rime word timestamps are estimates, not measurements

Rime coda returns word timestamps that arrive in the same millisecond as the
first audio byte, sit on a fixed 180.53 ms grid, and do not match the audio that
is actually delivered. Measured by `scripts/preflight_rime.py`:

| clause | chars | predicted end | bytes-derived | drift | ratio |
|---|---|---|---|---|---|
| `sec-4b-vii` | 354 | 22205 ms | 24720 ms | 2515 ms | 0.90 |
| `sec-7b-ii` | 54 | 4513 ms | 4480 ms | 33 ms | 1.01 |
| `sec-5b-iv` | 62 | 4152 ms | 6320 ms | 2168 ms | 0.66 |

The error is not a function of length: two clauses of 54 and 62 characters land
at 1.01 and 0.66. It cannot be corrected for, only measured.

Three consequences, all enforced in code:

- **Audio length comes from bytes, never from timestamps.**
  `audio_ms = bytes / 2 / sample_rate * 1000`. Treating the prediction as the
  length made a clause look finished at roughly half its true duration, so the
  reader advanced early and the on-screen text ran ahead of the voice, gaining
  on it with every clause.
- **Heard and the delivery boundary come from client-acked frames** measured
  against that byte-derived length. A `timestamp_drift` event is written at each
  `Done` recording predicted vs actual, so the gap stays visible in the trace.
- **Highlight granularity is clause-level, not word-level.** On `Done` the word
  map is stretched linearly so its last word ends with the audio; every span is
  then marked `estimated` when the stretch exceeds 5%, which is almost always.
  The read-along is therefore an interpolation between two known points (clause
  start and clause end), and `/api/metrics` reports the interpolated-span count
  honestly rather than implying word-accurate alignment.

`preflight_rime.py` fails when drift exceeds 1500 ms on any of the three probe
clauses. Against Rime as it behaves today **that check fails**, deliberately: it
is a canary for the assumption, not a gate that is expected to pass.

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
- **Extraction is best-effort and fails silently.** Scanned or JS-rendered pages
  yield too little text and are rejected outright (< 500 chars), but a page that
  extracts *badly* still validates. Observed failure modes, all of which the
  `--review` step exists to catch: site chrome (feedback widgets, cookie banners)
  ingested as document text unless its class or id matches the block list in
  `extract_html`; a one-word paragraph such as `Example` absorbed into the
  following clause by the short-clause merge; and list introducers borrowed only
  by bullets under six words, so a single list can end up internally inconsistent.
- **A lone `(i)` is ambiguous** — roman one, or the ninth letter. The segmenter
  guesses from context (roman unless it follows `(h)`), which is right for legal
  numbering and wrong for a document that genuinely runs `(a)`…`(i)`.
- **Ingested fixtures have no spoken section number.** Unnumbered documents get
  `sec-<n>-p<k>` ids, meaningless read aloud, so `Hit.citation_spoken()` cites the
  heading instead. That yields "That's covered further down, in If you're not
  eligible" — grammatical, clumsy.
- **Table clauses are spoken on request only.** The structure pass turns a
  table into one `table_stub` clause ("There is a table here: ... Ask me for
  any row.") and one `table_row` clause per row flagged `spoken_on_request`.
  Linear playback skips the rows and logs `unit_skipped` for each; a question
  can still land on a row. A grid is never read cell by cell.
- **Definitions grids are linearised by the layout model.** Docling flattens a
  definitions table into rows; the term index is built from those rows and from
  "X means ..." lead-ins. The Bharat Griha Raksha definitions were verified
  against the source document by hand, not by a test.
- **Uploaded documents are unreviewed** until a person presses Accept, and may
  carry residual boilerplate the regex pass did not catch; the developer page
  lists the first fifteen demotions so that is checkable.
- **Retrieval quality is not claimed.** What is claimed is that the twenty
  scripted questions in `fixtures/policy.questions.json` resolve to the
  expected clause by the expected branch (`scripts/check_grounding.py`). The
  BM25 with a synonym table and a proximity prior is not a retrieval feature,
  and no embeddings or reranker are used; a TODO in `grounding.py` records the
  plan if a scripted question ever misses. The claim holds on reviewed
  fixtures only.
- **The eligibility refusal is insurance-worded.** It ends "contact the insurer or
  lender", the wrong referral on a government-scheme fixture. The sentence is
  fixed verbatim by the brief; a per-fixture referral string would be the fix.

## Failure behaviour

| Failure | Behaviour |
|---|---|
| Rime socket drops mid-unit | `provider_disconnected` logged; in-flight iterators receive `TTSError`; agent reconnects and resumes from the last acknowledged boundary |
| Late audio after cancel | dropped, `result_fenced` with byte count |
| Boilerplate or table row in reading order | never sent to Rime; one `unit_skipped` per clause at session start with `reason: boilerplate` or `reason: table_on_request`, so the session record is heard / truncated / never-sent / skipped with nothing absent |
| Rime sends an odd-length PCM chunk | trailing byte carried into the next chunk, in the adapter and in the browser; `chunk_realigned` per unit with the odd-chunk count; a lone final byte is dropped and logged |
| Client frame count short of the bytes sent | unit is not marked heard; `frame_count_mismatch` with both counts. Heard is client-acknowledged, never server-estimated |
| Speaker not on live catalog | `fetch_voices.py` exits 1 — submission blocker |
| Question about unread clause | offer to jump; clause not read |
| Question not answerable from text | says so, redirects to insurer |
| LLM unavailable | extractive answer: cite and re-read the clause |
