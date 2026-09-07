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

# Optional, build time only: the navigator's generated fields (scripts/enrich.py).
# ENRICH_PROVIDER=none (default) skips it and the reader uses the mechanical map.
ollama pull granite4.2:3b                 # local: ENRICH_PROVIDER=ollama; same model as the runtime answers; the hero fixture enriches in ~1 min
#   or   ENRICH_PROVIDER=groq GROQ_API_KEY=... GROQ_MODEL=<from GET /openai/v1/models>   # hosted free tier
python scripts/enrich.py examples/policy-reader/fixtures/policy.json   # optional: pre-generate; otherwise it happens on entry (upload / first open)
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
| Ollama (`granite4.2:3b`) | runtime, optional | local answer model behind grounded Q&A; no key; absent == extractive answers |
| `docling` | build time only | structure pass; runs locally, no API, pinned in `requirements-build.txt`; models downloaded once with `docling-tools models download` |
| `openai` client (Groq) / Ollama HTTP | build time only | `scripts/enrich.py`; Groq is a hosted third-party service used only during fixture preparation, never at runtime; Ollama is local |

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

### Upload cleaning (text dumps, headings, front matter)

A PDF dumped to text is not the document: it carries `=== PAGE n ===`
markers, "Page x of y", lines of dots or dashes, form blanks (underscore runs)
and checkbox glyphs (the Unicode box set and the Private Use Area that
Wingdings characters land in). `scripts/ingest.py` cleans every `.txt`/`.md`
(and legacy PDF) line before segmentation -- `clean_lines()`: page markers and
"Page x of y" go, punctuation/underscore/glyph-only lines go, glyphs inside a
line go, `___` runs read as "blank"; a dropped line leaves a blank line so the
paragraphs either side stay apart. A dump with page markers is split into
pages (`split_pages`) so running headers and footers are stripped across
pages for text exactly as for PDF (`drop_repeated_lines`). The heading
heuristic for plain text never makes a heading from a single word, a form
field ("APPLICATION NO.: ____"), a line that is mostly a number (a phone, a
pin code), one dangling on "/" or a dash, one with table symbols (`> = %`),
one that repeats a word ("Nil Nil Nil"), or one followed directly by another
heading -- those were rows of a dumped table. In the structure pass a body
line before the first numbered heading that is a URL, a phone number, a UIN,
a CIN or an ISO reference is `boilerplate` (`front_matter`, bounded to the
first 60 blocks so a document with no numbered heading keeps its contact
section). Each rule has a test in `tests/test_ingest.py` /
`tests/test_ingest_structure.py`; the home-loan text dump re-ingests with
26 headings (was 65, twelve of them page markers) and zero
"childless heading" warnings.

Every fixture also carries a **spoken title** (`spoken_title` in the fixture
and in `index.json`): the docx title property when the file has one, else the
first real heading the structure pass found, else the title cleaned of
underscores, UINs and "(SYNTHETIC FIXTURE)" and title-cased. It is what the
voice says in every line that names the document, and it is editable on `/dev`.
`scripts/ingest.py` skips a source whose bytes are already registered unless
`--force`; a forced re-ingest keeps what the model generated where it still
fits (`carry_enrichment`: briefs and questions by section title, tags where the
clause text is unchanged, the overview and topics when the section list is the
same) so a re-ingest for cleaning does not throw away an enrichment.

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

### Delete a document

`DELETE /documents/{doc_id}` takes a document out of `index.json`, removes its
fixture, Docling JSON, ingest report and table CSVs (and the `.incoming_*`
source if it is still there), closes its session, and if it was the open
document opens the next one. Every tab gets `document_deleted {name, doc_id,
current, documents}`. The committed fixtures are refused (403) unless
`?force=1`. The listener rail shows a trash icon on unreviewed entries (with a
confirm); `/dev` has Delete on every entry. Uploads are deduplicated twice:
the same bytes return the existing entry (as before), and the same text under
other bytes -- a re-export, a different line wrap -- is a **409** with the
existing entry and a companion line, "That looks like {title}, which is
already here."
(`text_sha256` in the entry's source block: a hash of the text with case,
whitespace and punctuation removed, computed by the light extractors at ingest
and at upload). Tests: `tests/test_server_delete.py`,
`web/src/components/UploadDocument.test.tsx`.

## Navigator (build-time enrichment)

The navigator **describes and extracts; it does not interpret or recommend.**
`scripts/enrich.py` runs after ingestion, at build time only, and writes
generated fields into the fixture, every one marked `generated: true` with the
provider's name: a document `overview` (what it is, who issues it, how it is
organised, plus a listening time computed from clause length and the measured
speech rate in the traces), one descriptive `brief` per top-level section with
`est_minutes`, `tags` per clause from a fixed set (`exclusion`,
`waiting_period`, `deadline`, `amount`, `obligation`, `definition`,
`procedure`, `contact`), two or three `suggested_questions` per section (each
pointing at a clause id that must exist in that section or it is dropped),
`topics` chips from a fixed domain list resolved to headings that exist (a
heading that names the topic always wins over the model's choice), and spoken
descriptions for tables. An output guard rejects advisory or ranking language
(`you should`, `important`, `make sure`, `recommend`, `beware`, `key thing`,
`crucial`, `must know`, `be careful`); one regeneration, then the mechanical
form, with the rejection logged in the ingest report and shown on `/dev`.

Provider is `ollama` (local; `granite4.2:3b` by default, one pull serves both enrichment and runtime answers) or `groq` (hosted; an
OpenAI-compatible free tier, disclosed here as a third-party service used only
during fixture preparation and never in the judged runtime path; the model
name is never in source, it comes from `GROQ_MODEL` and is checked against
`GET /openai/v1/models` at start; ~30 requests/min and ~6,000 tokens/min, so a
200-clause document takes a few minutes) or `none` (nothing generated; the
reader speaks the mechanical map). Both model providers use the same prompts
and the same parser, so the schema never changes with the provider.

At runtime the reader speaks the overview on open while the topic chips show;
a chip (or a spoken topic name) asks "Read it now, or hear the rest of the
overview first?"; section transitions speak the brief as the coming-up cue
("skip it" jumps on, "go on" continues); after a section is heard one
suggested question is offered ("People usually ask here whether … Want that?",
answered from its stored clause with no retrieval); and extractive asks --
"read me every exclusion", "what deadlines are in this document", "summarise
this section" -- return cited clauses from the tags, never ranked, no model.
Every jump goes through the interruption path (see the failure table:
`jump`). None of this calls a model at runtime.

### Enrichment scheduling (runtime)

The navigator pass (overview, briefs, topics) has its own lock and priority.
The rest pass (tags, questions, tables) runs one model call at a time
(`Enricher.steps()`: a tag batch, one section's questions, one table) and
between calls yields while a navigator is waiting, an answer is in flight or
a prompt is open, so a question asked during the rest pass is answered within
one step and a second document's navigator lands while the first document's
rest pass is mid-way. The fixture is written after every step, so progress
survives a restart. `ENRICH_REST_ENABLED` (default true) and a `/dev` toggle
switch the rest pass off, which keeps the model free for answers; the status
row `enrich_rest` shows the switch and how many navigators are waiting. Trace:
`enrich_step{step, index, of, ms}`, `enrich_rest_skipped`,
`enrich_rest_toggled`. Test: `tests/test_enrich_sched.py`.

### Conversation (prompts, starters, tables)

The reader talks, and listens for the reply. Each prompt is a spoken unit of
its own `kind`, heard on the client's acks like a clause, and only then gives
the listener the floor for a fixed number of seconds before its default. At
most one prompt is open at a time; opening another resolves the old one as
`superseded`; a cue may follow a prompt, another prompt may not. A kind the
listener lets time out twice in a session is muted for the rest of it
(`prompt_muted`). The trace carries `prompt_opened{kind, options}` and
`prompt_resolved{kind, by: reply|timeout|chip|superseded|cancelled, choice}`.

**How a reply is read.** Every typed utterance goes through `route_reply`
(`conversation.py`), in this order, and the trace says which step took it
(`reply_understood{intent, section_id, row, via: rules|llm, ms}`):

1. an exact option word, chip text or navigation phrase for the open prompt
   -> the rules, no model;
2. `llm.understand(text, ctx)`: the model returns intent and slots in a closed
   schema (`topic | question | brief | start | carry_on | skip | back | row |
   all | yes | no | recap | repeat | unclear`, plus a `section_id` from the
   section list it was shown, a `row` from the labels, a cleaned `question`);
   anything outside the lists is dropped and the reply is `unclear`;
3. execution stays deterministic: a topic goes `find_section` first, else the
   model's `section_id`, then the cue "{heading}, about {m} minutes.
   Starting." and the jump; a question takes the ask path; every other intent
   is read against the open prompt's options or takes its global action;
4. the floor: no model, a timeout (4 s) or `unclear` -> the v1 rules exactly,
   including the closed-set classifier for an open prompt (`via: rules`).

| Prompt | Says | Options -> action | Silence | Read by |
|---|---|---|---|---|
| `welcome` (8 s) | "I can read a policy or agreement to you and answer questions as we go. You have {n} here: {titles}. Which one, or upload a new one?" On the first play of a session with nothing started and more than one document. | a title -> open -> `start_choice`; "upload" -> the upload dialog (`focus_upload`) | the first document, read | title by rules (exact, substring, overlap), then the model |
| `ingest_wait` (no timer; closes at ready) | "I'm going through the document now, about a minute. While I do: is there something you want to know from it? I'll look for it first." Upload starts, voice claimed. Then the cues "Got it, I'll look for that.", "Found {S} sections.", "Nearly there. I'm putting an overview together." | anything said -> `parked_question`; at ready: "First, what you asked while I was reading it." -> the answer over the whole document -> `start_choice` | none | rules (everything is the question) |
| `start_choice` (8 s) | "I've gone through {title}, about {N} minutes to read. Is there a topic you have in mind? If not, I'll give you a brief and you can pick from there." | a topic -> `confirm_topic`; "brief" / "no" -> the overview and chips; "start" -> read from the top, no overview; a question -> answered, then the invitation once more | the overview and chips | chip text by rules; anything else by the model |
| `confirm_topic` (6 s) | "{heading}, about {m} minutes. Read it now?" | "yes" -> jump; "no" -> the overview | the overview | rules, then the model |
| `pick_topic` (8 s) | "Where shall we start: {topic1}, {topic2}, {topic3}, or from the top?" After the overview is heard. | a topic -> jump; "top" -> read on | from the top | chip text by rules; the model otherwise |
| `choice` (8 s) | "{topic} is {heading}, about {m} minutes. Read it now, or hear the rest of the overview first?" After a chip. | "now" / "overview first" | "I'll read from the start. Interrupt me any time." | rules, then the model |
| `offer` (6 s) | "People usually ask here whether … Want that?" After a section is heard. | "yes" -> the stored clause; "no" / "go on" | reading continues | rules, then the model |
| `section_end` (5 s) | "That's {heading}. Next is {next}. Carry on, or something else?" At a top-level boundary the listener heard to its end; never within `SECTION_END_MIN_GAP_S` (180 s) of the last prompt, never right after a jump the listener asked for, never when an offer was just made. | "carry on"; a topic -> jump; a question | carry on | rules, then the model |
| `table_choice` (6 s) | "Here there's a table of {description}, {n} rows: {first six labels}. Want one of them, all of them, or shall I carry on?" | a label or ordinal -> that row as a `row` unit, then "Another, or carry on?"; "all" -> the rows in order; "carry on" | carry on | labels and ordinals by rules; the model otherwise |
| `not_found` (6 s) | "I couldn't find that in this document; the insurer or lender can tell you. Carry on, or try another word?" After a question the document does not answer. | "carry on"; a question | carry on | rules, then the model |
| resume cue | "We were in {heading}. Carrying on." On play after a pause longer than `RESUME_CUE_AFTER_S` (120 s). | -- | -- | -- |
| `end_choice` (8 s) | "That's the end. Want any section again, or a recap of what we covered?" | a section -> jump; "recap" -> the sections heard in full, partly heard and not heard, built from the ledger, no model | stop | rules, then the model |

Rows spoken on request are in the ledger as `heard` like clauses; rows never
asked for stay `skipped:table_on_request`. Tables enrichment marked
`read_inline` are read without a prompt. Play pressed over an invitation or a
choice closes it and reads. Minutes come from the measured speech rate
(`CHARS_PER_SECOND`). Cues and prompts are spoken one after another under one
lock; a prompt closed while still streaming sends a terminal `unit_done{cut}`.

### Companion (no radio silence while a document is processed)

Two roles, one boundary. The **companion** keeps the line alive while a
document is ingested and enriched; the **reader** answers from the document,
unchanged.

| | Companion | Reader |
|---|---|---|
| speaks | progress, the engagement question, an acknowledgement, a plan, fillers | clauses, grounded answers, the overview and briefs |
| may see | title, page and section counts, section titles, stage names, the listener's own words, the ledger | clause text (retrieved), heard-so-far text, the question |
| may never | state or paraphrase anything the document says; numbers, amounts, percentages, section citations | small talk |
| text from | templates; a model only to reword an acknowledgement | `grounding.resolve` and the answer prompt |
| model | `COMPANION_MODEL` (default: the answer model) | `LLM_MODEL` |
| guard | `companion_guard` | the no-interpretation prompt and the eligibility refusal |

While a tab has the voice, `Narrator` (`companion.py`) speaks, as `companion`
units under the same lock as prompts: real progress as the stage events
arrive ("Got the text, {pages} pages." -> "I can see {S} sections: {first
three}, and more." -> "Checking the wording and any personal details." ->
"Nearly there, putting an overview together." -> "Done."), the one engagement
question at about five seconds ("While I do this, is there anything you want
to know from it? I'll look for it first.", the `ingest_wait` prompt), the
acknowledgement on a reply ("Okay, I'll keep that in mind: {topic phrase}."),
the plan line at twenty seconds without one ("Okay. Once I'm done I'll give
you an overview, and the main sections will show up below.", then no more
questions that ingest), and fillers only after twelve seconds without a line
(rotating, never repeated in a session, at most five per ingest). Any listener
reply cancels the line in flight; nothing is said over a prompt that is
waiting; a real event pre-empts a queued filler. The section list goes to the
client at the structure stage (`sections_found`), before enrichment.

`companion_guard` rejects a line with a digit, a percentage or currency, the
words "section" or "clause", or any six-word span found in the document's
clause text; it gates every model-phrased line (a rejection speaks the
template). Templates are trusted and carry only counts and titles. The parked
question is stored, never answered by the companion: at ready, "First, what you
asked earlier.", then the reader path with `answer_source` and
`retrieval_path` in the trace, then the invitation, which names the topic
phrase. Trace: `companion_spoken{source: event|question|ack|plan|filler|ready,
origin: template|model, text}`, `sections_found{n, titles}`, and per ingest
`narration_gap_ms{max, count, gaps}`, the longest silence (target p95 under 15
s; see RIME_EVIDENCE.md).

VRAM, 6 GB: Granite (2.2 GB) and Qwen 3.5 4B (3.4 GB) do not co-reside with
their KV caches; Ollama swaps, one to two seconds each. Enrichment needs
Granite in the same window a Qwen companion would use, so a second model buys
little during ingest and costs a swap per reply after it. The shipped path is
one model; `COMPANION_MODEL` is a switch to try (a Qwen model is sent with
thinking off). Templates need no model at all.

**The model's roles, exhaustively.** (1) Phrasing an in-scope or deictic
answer from the retrieved clause text (`SYSTEM_PROMPT`; "what does that mean"
restates the clause in plain everyday words, adding nothing). (2) Enrichment
at build time or on entry (`scripts/enrich.py`). (3) Understanding a reply
that is not an exact option word (`llm.understand`: intent and slots from the
lists it was given; JSON, temperature 0, `max_tokens` 80, 4 s; any failure is
`unclear`). (4) Mapping a named topic to one section id from the list, or
none (`llm.map_topic`). (5) As the floor only, classifying a reply into an
open prompt's options plus "question" (`llm.classify`: 20 tokens, 5 s; any
failure is "question"). (6) Rewording one acknowledgement for the companion
(`companion.phrase_ack`, 40 tokens, 3 s, behind `companion_guard`). Never
eligibility, never a jump target outside the section list, never a word of
document text, never the recap.

### The voice takes over for processing (both paths)

The interactive voice runs whenever a document is processed -- an upload, or
the enrichment of a document just opened -- and takes the voice over if it
has to (`ReaderSession.take_voice_for(doc, reason)`): whatever is sounding is
stopped exactly like an interrupt (boundary at the playhead, position saved,
`unit_truncated{reason: upload|open}`), an open prompt is closed as
`superseded`, and the companion says "I'll pause here and go through
{spoken title}." Then the narration plan, all templates, no model:
"Got the text, {pages} pages." at extract -> "I can see {S} sections: {first
three}, and more." at structure (the headings go to the client as
`sections_found`, shown as "Coming up" until the chips land) -> the engagement
question at ~5 s ("While I do this, is there anything you want to know from
it? I'll look for it first."; a reply is acknowledged "Okay, I'll keep that in
mind: {phrase}." and parked for that document; 20 s without one: "Okay. Once
I'm done I'll give you an overview, and the sections will show up below.") ->
a filler after every 12 s of silence (five templates in rotation, never a
document fact, for as long as the generation takes) -> "Nearly there, putting
an overview together." when enrichment starts -> "Done." -> the parked
question answered through the reader path -> `start_choice`. On the open
path the structure line follows the takeover line at once. Every line and
prompt is bound to the document being processed (`companion_spoken{document}`,
`narration_gap_ms{document, owner}`), never to whatever is open, and opening
another document does not close that document's `ingest_wait`. With no tab
holding the voice nothing is said (`narration_pending`); the first play is
told the backlog first: "While you were away I went through {title}. It has
{S} sections." (`backlog_spoken`). `narration_gap_ms.max` is the measure;
`scripts/qa_live.py` fails its pass when it exceeds 15 s.

**The live pass, repeatable.** `python scripts/qa_live.py --serve --tts fake`
(then `--tts rime`) starts a server on port 8090, claims the voice, starts
reading `saral_jeevan_bima`, uploads `fixtures/source/fixture_arogya_sanjeevani.docx`
converted to a plain PDF while that is being read, answers the engagement
question, waits for `start_choice`, replies "exclusions", reads into a table
and picks "the second one", and deletes the upload -- printing every companion
and prompt line with a timestamp, `narration_gap_ms`, `topics_offered.n`, the
uploaded fixture's headings and the delete result. Exit 1 on a silence over
15 s, junk in the headings, no table, or a delete that left files behind. The
enrichment guard also refuses a generated line that carries a clause id
("mentioned in sec-1-p2"), seen once in that pass.

`start_choice` is asked once per document: for a document opened from the
rail, 1.5 s after the last open (a run of clicks asks once, for the document
that stays; `start_choice_due` in the trace); never for the page re-opening the
document it shows at load; otherwise on the first play. It says
"I've gone through {spoken title}: {S} sections." -- the whole-document minutes
are gone; `confirm_topic` keeps its per-section minutes.

Side units -- the overview, the invitations, the welcome, companion lines, the
recap -- are one unit to the client but one Rime context per sentence
(`_stream_sentences`, the same sentence split as clauses), so an interrupt
mid-overview fences at most one sentence's bytes instead of the whole overview
streaming on. `/api/metrics` reports `fenced_bytes_session` and
`fenced_unit_bytes`. `/dev` lists every guard rejection (field, reason, the
mechanical text read instead) and shows the ingest report for the open
document.

**Chips, five to seven of them.** `Grounding.topics` is the generated topics
whose pointer resolves, then top-level section headings (cleaned of numbering
and "N items", four words at most) until there are five, then sub-headings
for a document with few sections, then "Read from the start"; never more than
seven, and `topics_offered.n` is at least five for every fixture in the index
(`tests/test_grounding.py`). A sub-heading chip jumps to its own span
(`Grounding.section_for_id`). `document_opened` carries the chips of a
document that has its navigator, `navigator ready` and `hello` too, and the
client never clears the chips for the document it is already showing, so an
open, a re-open at load, or a prompt no longer blanks them; they show during
`start_choice` as well. A `table_choice` or `offer` left behind by an open or
a jump closes with `by: navigated`. The table prompt speaks the generated
description as-is ("The table is ... Want one of them, all of them, or shall I
carry on?") instead of "Here there's a table of The table is ...".

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
`ingest_document()` in a worker thread and streams the eight stages as
server-sent events `{stage, status, elapsed_ms}`; the last event carries the
library entry `{doc_id, title, reviewed, readable, clause_count}`. The eighth
stage, `enrich`, runs **on entry**: with `ENRICH_PROVIDER` set, the overview,
section briefs and topic chips are generated before `done` (the trace carries
`enrich_started` / `enrich_done stage=navigator`, with elapsed time), and the
remaining fields -- tags, suggested questions, table shapes -- follow in the
background (`enrich_done stage=rest`). Without a provider the stage is
`skipped`; if generation fails it is `error`, the document still enters and
play uses the mechanical map. The entry is appended to `index.json` at once
with `reviewed: false`, and the reader can start on it. Opening a fixture that
has no navigator (the committed wordings before their first open on a machine
with a provider) starts the same generation; the rail says "Preparing
overview…", and play at the top of the document speaks a short cue and waits
up to `NAVIGATOR_WAIT_S` (120) for it rather than reading blind. The listener page renders only a progress bar keyed to the
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
the flush ack named, never the last clause synthesised under lookahead. In-scope and deictic answers go through the model named by `LLM_PROVIDER` /
`LLM_MODEL` when one is configured (key server-side only; `/api/status` shows
which) and the trace records `answer_source`; without one, or if the call
fails or exceeds `LLM_TIMEOUT_S`, the answer is extractive and the trace says
`llm_failed`. Eligibility questions never go through the model.

Every utterance the listener types (the same path when speech lands) goes
through one router, `route_reply`, in a fixed order, and the trace says which
step took it (`reply_routed{route: pending|nav|question|llm_classify}`):
first the open prompt's own option grammar (deterministic); then the
navigation regexes, where a bare topic name ("premium", "exclusions") jumps
but anything shaped like a question ("what is the premium", "explain
exclusions", a "?") never does; then a question, resolved by `grounding.py`;
and only when a prompt is open and nothing above matched, the model
classifies the reply into that prompt's options plus "question" (closed set,
JSON, temperature 0, 20 tokens, 5 s; any failure is "question"). A reply
never reaches the model when a rule matched, and the model never chooses a
jump target the rules did not offer.

**Local answer model (shipped path).** `LLM_PROVIDER=ollama` with
`LLM_MODEL=granite4.2:3b` talks to a local Ollama server over its
OpenAI-compatible endpoint and needs no key. Granite 4.2 3B was chosen
because it is tuned for answering from supplied passages, its 2.2 GB of
weights leave room for the KV cache on a 6 GB laptop GPU, and it does not
reason before it speaks, so the listener is not left waiting. Every request
is `temperature 0` and capped at `LLM_MAX_TOKENS`. At start the server runs
`check_llm()` (is the model present? if not the trace carries the exact
`ollama pull`) and `warm_llm()` (one-token request so the first question does
not pay the model load), emitting `llm_ready` or `llm_unavailable`.

```bash
ollama pull granite4.2:3b                         # once; on Windows or in WSL, wherever Ollama runs
python examples/policy-reader/llm.py --check      # reachable, present, warm, one timed answer
```

From WSL2 with Ollama on the Windows side, set `LLM_BASE_URL` to the Windows
host IP (`ip route | awk '/default/ {print $3}'`) if `localhost:11434` does
not reach it.

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
- **Generated text is reviewed by hand on the committed fixtures only.**
  Uploaded documents get enrichment only if a provider is configured on the
  machine that runs `scripts/enrich.py`; otherwise they open with the
  mechanical map and no chips, briefs or tags.
- **Scope note.** To pay for the navigator and the jump rewrite, the
  generality claim is made on two fixtures (the hero policy and the two-wheeler
  loan agreement) and the interruption drift script is run at 15 points rather
  than 20.
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
| Jump (chip, spoken topic, spoiler-gate offer, "skip it", "go back") | the interruption path with a different target: `unit_truncated` at the client boundary, `unit_skipped` with `reason: jump` for every clause passed over, `position_saved` (before_jump), `position_restored` (jump), a `jump` event `{from_unit, to_unit, reason, turn_id}`, then a cue unit and the target |
| Rime sends an odd-length PCM chunk | trailing byte carried into the next chunk, in the adapter and in the browser; `chunk_realigned` per unit with the odd-chunk count; a lone final byte is dropped and logged |
| Client frame count short of the bytes sent | unit is not marked heard; `frame_count_mismatch` with both counts. Heard is client-acknowledged, never server-estimated |
| Speaker not on live catalog | `fetch_voices.py` exits 1 — submission blocker |
| Question about unread clause | offer to jump; clause not read |
| Question not answerable from text | says so, redirects to insurer |
| LLM unavailable, slow (> `LLM_TIMEOUT_S`) or model not pulled | extractive answer: cite and re-read the clause; trace `llm_failed` / `llm_unavailable`, `/api/status` shows warn |
