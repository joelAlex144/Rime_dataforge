# CLAUDE.md — rime-delivery-layer

Standing context for any Claude session working in this repo. Read before
changing anything. The hackathon brief, judging weights and design decisions
live in the project context; this file is the repo-level view.

## What this is

A **delivery-aware position layer** for voice agents: it tracks what the
listener actually heard (client-acknowledged, never server-estimated), fences
stale results by `turn_id`, and resumes reading at the sentence containing
the cut. The policy reader in `examples/policy-reader/` is the environment
that makes the failure obvious; it is not the product.

Rime is the primary TTS in the judged flow (`coda` / `bancroft` / `en` /
`pcm` 24 kHz over `wss://users-ws.rime.ai/ws3`). `TTS_PROVIDER=fake` is the
disclosed offline fallback; the active provider is always in the trace as
`provider_active`.

## Layout

```
delivery_layer/          the layer: ledger, fence, position, scheduler, wordmap, tts adapters
client/                  browser worklet + SDK that acks rendered frames (the only source of truth)
examples/policy-reader/  agent, server (aiohttp), grounding, llm, ingest, React web UI
  fixtures/              five hand-checked IRDAI / lender wordings, pre-chunked, reviewed: true
  acceptance/            scripted interruption harness and far-end measurement
scripts/                 fetch_voices, preflight_rime, ingest, number_roundtrip
tests/                   offline; no key needed
traces/                  committed evidence (session_web-*.jsonl, preflight_*)
docs/                    handoff notes between the synthesis and delivery slices
```

## Run

```bash
source .venv311/bin/activate         # WSL venv (python 3.11); run from WSL, not PowerShell
set -a; source .env; set +a          # never commit .env; never paste a key anywhere
python -m pytest                     # offline
python examples/policy-reader/llm.py --check          # answer model reachable + warm
python examples/policy-reader/server.py               # :8080, real Rime voice
cd examples/policy-reader/web && npm run dev          # :5173 listener, /dev diagnostics
```

`--dev` enables provider swapping and the diagnostics controls. Never for the
judged flow.

## Answer model (grounded Q&A)

The PDF never touches the LLM. Ingest is docling + deterministic chunking;
retrieval is `grounding.py`'s own clause lookup. The model only sees the
system prompt, one to three retrieved clauses, an optional "listener heard so
far … [interrupted]" block, and the question, and must return one or two
spoken sentences with no interpretation. Eligibility questions are answered
deterministically and never reach the model.

Shipped local path: **`LLM_PROVIDER=ollama`, `LLM_MODEL=granite4.2:3b`**
(`ollama pull granite4.2:3b`, 2.2 GB, fits a 6 GB laptop GPU beside its KV
cache, RAG-tuned, no thinking loop before the answer). Requests are
`temperature 0`, `max_tokens` = `LLM_MAX_TOKENS` (256), timeout
`LLM_TIMEOUT_S` (30 s). Server start runs `check_llm()` then `warm_llm()`
and emits `llm_ready` / `llm_unavailable`; `/api/status` shows the model or a
warn cell. Any failure falls back to the extractive answer and the trace
records `llm_failed` and `answer_source`.

Alternatives: `LLM_PROVIDER=anthropic|openai` with `LLM_API_KEY`;
`LLM_BASE_URL` points `ollama`/`openai` at any OpenAI-compatible server.
From WSL2 with Ollama on Windows, set `LLM_BASE_URL` to the Windows host IP
if `localhost:11434` is not reachable. Do not switch to a thinking-by-default
model (e.g. `qwen3.5:4b`) without also disabling thinking in `llm.py`; the
answer is on the voice line.

## Navigator (overview + "what's ahead", build time)

The listener does not get a blind read. Enrichment happens **on entry**, not
as a separate step: the `enrich` upload stage (after `write`, before `done`)
and the first open of a fixture without a navigator both call
`enrich_fixture()` from `scripts/enrich.py` (`ENRICH_PROVIDER=ollama`,
`OLLAMA_MODEL=granite4.2:3b`, the same pull as the answer model). It writes
generated fields into the fixture:
`overview` (what the document is and its sections in order), `sections`
(heading, brief, `est_minutes`), `topics` (4-5 plain-language chips mapped to
sections, plus "Read from the start") and `suggested_questions`. Every string
passes an output guard (no advice, no ranking, no second person); a failure
regenerates once, then falls back to the mechanical form and is logged in the
ingest report. All generated fields carry `generated: true`.

At runtime, play at position 0 broadcasts the topic chips and speaks the
overview through Rime as a `map` unit (interruptible, client-acked). A chip
stops the voice, names the section and its minutes, and asks "Read it now, or
hear the rest of the overview first?" (8 s, default: read from the start).
Decision: curated topics only on the listener page; the full section list is
on `/dev`. Nothing generated is read as document text and nothing from the
LLM runs in the judged read loop.

Order: `NAVIGATOR_FIELDS` (overview, sections, topics) are generated before
the upload reports `done` or before play speaks; tags, suggested questions and
table shapes follow in the background and are picked up at the next open.
Play at the top of a document whose navigator is still generating speaks a
cue and waits up to `NAVIGATOR_WAIT_S` (120 s); past that, or on failure,
the mechanical map. Events: `enrich_started`, `enrich_done` (stage
navigator|rest), `enrich_failed`, `enrich_timeout`; rows carry
`navigator: ready|preparing|mechanical`. `scripts/enrich.py --all` still
works to pre-generate (e.g. before a demo, so no wait) and to review
generated overviews on `/dev` first. Tests: `tests/test_server_navigator.py`
(model faked).

Scheduling (2026-09-07): the navigator pass has its own lock
(`_navigator_lock`) and priority (`_navigator_waiting`); the rest pass runs
one model call per executor step (`Enricher.steps()` / `finish()`) and yields
between steps while a navigator waits, `_answer` is in flight or a prompt is
open (`_rest_may_run`). `ENRICH_REST_ENABLED` (default true), session flag
`enrich_rest_enabled`, `POST /api/dev/enrich_rest`, status cell `enrich_rest`.
Opening an unenriched document is narrated (`narrate_enrichment`, owner
"open") exactly like an upload. Test: `tests/test_enrich_sched.py`.

## Conversation layer (built 2026-09-07) — starters, reply routing, tables

Status: built as designed below, on the existing interruption path and the
existing ledger. `route_reply` in server.py is the one router; `open_prompt` /
`resolve_prompt` the one prompt stack (`PROMPT_TIMEOUT_S`); `llm.classify` and
`llm.map_topic` the two closed-set model calls. Additions found necessary
while building: side units (cues, prompts) are spoken one after another under
`speak_lock`, never interleaved; a prompt closed while still streaming sends a
terminal `unit_done{cut: true}` and is fenced, so every `unit_started` has an
end; play pressed over an invitation closes it (`by: chip`) and reads. Tests:
`tests/test_server_start_choice.py`, `tests/test_server_table.py`,
`tests/test_server_navigator.py` (ingest cues), `tests/test_llm.py`
(classify / map_topic), `tests/test_grounding_routing.py` and
`tests/test_grounding.py` (the two root-cause fixes). README: "Conversation".

Three reported problems and their causes, from reading the code (kept as the
record of why):

1. **"I interrupt and it keeps reading the paragraph instead of answering."**
   Two causes. (a) `grounding.navigation_intent()` ends with a bare-topic rule:
   any utterance of <= 5 words whose content tokens overlap a section title at
   >= 0.6 becomes `goto` and the reader JUMPS ("what is the premium" ->
   Premium section). It is matched before the reader is stopped and before
   retrieval. (b) With no answer model configured, a deictic answer is the
   extractive form -- "Section X, says: <the clause>" -- i.e. the paragraph
   just heard, re-read, then resume at the same sentence: it sounds like
   nothing happened. Granite fixes (b) only if the prompt stops telling the
   model to "use the document's own words" for a *what does that mean*.
2. **Tables.** Linear playback speaks the stub ("There is a table here: 8
   entries, X to Y. Ask me for any row.") and skips the rows
   (`spoken_on_request`); nothing listens for the reply and there is no
   "read row / read all / carry on" intent.
3. **Processing is silent.** Upload streams stages to the screen but the voice
   says nothing, and when the document is ready nothing invites the listener.

### Design (one router, one prompt stack)

**Router.** Every listener utterance (typed today; the same path when STT
lands) goes through `route_reply(text, pending)` in server.py, in this order:
  1. *Pending prompt grammar.* If a prompt is open, interpret the reply
     against its option set first (see prompts below). Deterministic.
  2. *Navigation regexes* (existing `navigation_intent`) with the bare-topic
     rule tightened: only when the utterance has NO question word
     (what/how/why/when/which/does/is/can/explain/mean/tell/…) and no `?`.
  3. *Question* -> `grounding.resolve()` -> deterministic branches
     (beyond_cursor / not_found / eligibility) or LLM phrasing.
  4. *Closed-set fallback.* If a prompt is pending and 1-2 did not match, ask
     Granite to classify the reply into that prompt's options plus
     `"question"` (JSON, temperature 0, max_tokens 20, 5 s timeout). Never a
     free-text navigation decision; on timeout treat as `question`.
The LLM's roles, exhaustively: answer phrasing; enrichment (build/entry);
closed-set reply classification; topic-to-section mapping when the listener
names a topic no heading matches (given the section list, pick one or
`none`). Never eligibility, never a jump target outside the section list.

**Prompts and starters** (each is a spoken unit with a `kind`, a pending
state, a timeout default, and a trace event `prompt_opened` /
`prompt_resolved{by: reply|timeout|chip}`):
  - `ingest_wait` (upload starts): "Please wait while I go through the
    document. This usually takes about a minute." At the `enrich` stage:
    "Nearly there -- putting an overview together." No pending state.
  - `start_choice` (document ready / first open): "I've gone through
    {title}, about {N} minutes to read. Is there a topic you have in mind?
    If not, I'll give you a brief and you can pick from there." Options:
    topic name -> `find_section`, else LLM topic map -> confirm "{heading},
    about {m} minutes -- read it now?"; "brief"/"no"/silence 8 s -> overview
    + chips (existing map unit); "start"/"read" -> read from the top;
    question -> answer, then re-open `start_choice` once.
  - `table_choice` (table stub reached while reading, table not
    `read_inline`): "Here there's a table of {description}, {n} rows:
    {first <= 6 row labels}. Want one of them, all of them, or shall I carry
    on?" Options: row label / ordinal -> speak that row (`table_row` unit),
    then "Another, or carry on?" (6 s); "all"/"every" -> rows inline in
    order; "carry on"/"no"/silence 6 s -> continue after the table. Tables
    with <= 6 rows and <= 3 columns keep `read_inline` and are read without
    asking.
  - existing `choice` (now / overview first) and `offer` (suggested
    question) are unchanged and join the same pending-state machine: at
    most ONE prompt open at a time; opening a new one resolves the old as
    `superseded`.

**Deictic answers.** Add to SYSTEM_PROMPT: for "what does that mean" /
"explain that", restate the clause in plain everyday words, adding nothing
that is not in the text; do not repeat it verbatim. Restatement is not
interpretation; advice and outcomes remain forbidden.

**Evidence.** Every prompt/reply pair is in the trace with the route taken
(`route: pending|nav|question|llm_classify`) so the demo can show that a
reply never reached the model when a rule matched.

## Conversation layer v2 (built 2026-09-07): LLM-first understanding, more moments

Status: built as designed below. `examples/policy-reader/conversation.py`
holds the prompt state and the four-step `route`; `llm.understand` is the
closed-schema call; `route_reply_rules` in server.py is v1, the floor. The
moments added: `welcome`, `ingest_wait` as a prompt with `parked_question`
(answered first at ready), `pick_topic` after the overview, `section_end`
(rate-limited, muted after two timeouts), `not_found`, the resume cue,
`end_choice` with the ledger recap. Tests: `tests/test_conversation.py`,
`tests/test_server_section_end.py`, `tests/test_server_recap.py`, and the
extended `test_server_start_choice.py` (pick_topic, welcome),
`test_server_navigator.py` (ingest_wait, parked question), `test_llm.py`
(understand). README "Conversation" lists every prompt with its text,
options, timeout, default and who reads the reply. Kept from building it: the
test helpers that play from the top answer `pick_topic` and `welcome`, since
both now stand between play and the first clause.

v1 (above) routes rules first and asks the model only to classify a reply
into an open prompt's options. v2 flips that for anything that is not an
exact option word: the model UNDERSTANDS the reply first (intent + slots,
closed schema), retrieval and actions stay deterministic, and the v1 route
is the floor when the model is unavailable, times out, or says `unclear`.
Nothing in delivery_layer/ or the interrupt/boundary/resume path changes.

### Understanding

`llm.understand(text, ctx) -> dict` (Granite, JSON only, temperature 0,
max_tokens 80, timeout 4 s). `ctx`: open prompt kind and its options, the
section list `[{id, title}]`, the row labels if a table prompt is open, the
last heard heading, and whether reading is in progress. Schema:

    {"intent": "topic|question|brief|start|carry_on|skip|back|row|all|
                yes|no|recap|repeat|unclear",
     "section_id": "<id from the list or null>",
     "row": "<label from the list or null>",
     "question": "<the question, cleaned up, or null>"}

Any `section_id` / `row` not in the supplied lists is dropped (-> `unclear`).
Trace `reply_understood{intent, section_id, row, via: llm|rules, ms}`.

`Conversation.route(text)` (replaces the body of `route_reply`):
  1. exact option word or chip text for the open prompt -> rules, no model;
  2. `understand()`;
  3. execute: topic -> `find_section(text)` first, else `section_id` -> cue
     "{heading}, about {m} minutes. Starting." -> jump; question -> the
     existing ask path (`grounding.resolve` -> answer, LLM phrasing; a
     `not_found` result takes the existing not-found route, now as the
     `not_found` prompt); every other intent -> its action;
  4. floor: model unavailable / timeout / `unclear` -> v1 `route_reply`
     exactly as built (`via: rules`).

### Moments added (all through `open_prompt` / `resolve_prompt`)

| moment | kind | says | options -> action | silence |
|---|---|---|---|---|
| first play, no document started, >1 in library | `welcome` | "I can read a policy or agreement to you and answer questions as we go. You have {n} here: {titles}. Which one, or upload a new one?" | title -> open -> `start_choice`; "upload" -> `focus_upload` to the client | 8 s -> first document |
| upload starts, sink claimed | `ingest_wait` (now a prompt) | "I'm going through the document now, about a minute. While I do: is there something you want to know from it? I'll look for it first." | question -> `parked_question`, cue "Got it, I'll look for that." | none; closes at ready |
| ready with a parked question | (auto) | "First, what you asked while I was reading it." -> answer -> `start_choice` | | |
| after the brief (overview + chips) | `pick_topic` | "Where shall we start: {topic1}, {topic2}, {topic3}, or from the top?" | topic -> jump; "top" -> read | 8 s -> from the top |
| end of a top-level section (rate-limited) | `section_end` | "That's {heading}. Next is {next}. Carry on, or something else?" | carry on; topic; question | 5 s -> carry on |
| answer not found | `not_found` | "I couldn't find that in this document; the insurer or lender can tell you. Carry on, or try another word?" | carry on; question | 6 s -> carry on |
| play after a pause > 2 min | cue | "We were in {heading}. Carrying on." | -- | -- |
| end of document | `end_choice` | "That's the end. Want any section again, or a recap of what we covered?" | section -> jump; "recap" -> spoken list of sections heard / partly heard / skipped, built FROM THE LEDGER, no model | 8 s -> stop |

Engagement rules: a prompt is <= 2 spoken sentences and names its default;
never two prompts back to back (a cue may follow a prompt, not another
prompt); `section_end` at most once per 3 minutes of reading and never
after a jump the listener just asked for; a prompt kind the listener let
time out twice in a session is muted for the rest of it (`prompt_muted`).

### Integration points (server.py, existing names)
- `route_reply` -> delegate to `Conversation.route`; keep the v1 body as
  `route_reply_rules` (the floor).
- `open_start_choice` unchanged; `pick_topic` opened by `_speak_map` after
  the map unit is heard (instead of the 8 s silent wait).
- `api_documents_post`: `ingest_wait` becomes a prompt with `parked_question`
  in the session; `_enrich` done + sink claimed -> parked answer, then
  `start_choice`.
- `read_loop`: at a top-level section boundary call `maybe_section_end()`;
  at end of document `open_end_choice()`; on `play` after a pause > 120 s
  speak the resume cue before the clause.
- `recap` reads `session.ledger` (heard / truncated@ / skipped:*) grouped by
  section; this is the observability claim spoken aloud.
- Web: prompt options as buttons (already); show "Heard as: {intent}
  {section title}" under the box from `reply_understood`.

## Companion (built 2026-09-07): no radio silence while the document is processed

Status: built as designed below. `examples/policy-reader/companion.py` holds
the templates, `companion_guard`, `phrase_ack` (the one model use, guarded,
`COMPANION_MODEL`) and `Narrator`; server.py drives it from
`api_documents_post` while a tab has the voice, speaks every line as a
`companion` unit under `speak_lock`, cancels the line in flight on any reply,
answers the parked question through the reader path at ready ("First, what
you asked earlier.") and names the parked phrase in `start_choice`. The
structure stage's `progress` callback now carries `pages` and the top-level
`headings`, so the section list reaches the client before enrichment
(`sections_found`). Tests: `tests/test_companion.py`, and
`tests/test_server_navigator.py` (slow stages with and without a reply, the
parked question answered only through grounding). README "Companion";
RIME_EVIDENCE "Narration gap". Found while building: the filler clock runs
from the last line heard, not from a silent stage event; and a test client
must keep reading the socket during a long upload, since the server's
heartbeat drops a tab that stops answering pings -- a browser always reads.

Observed on the current build: "I'm going through the document" -> 30-90 s of
silence (docling, then enrichment) -> "do you have a topic in mind". The
listener has nothing to hold on to. Fix: a **companion** role that keeps the
line alive with true progress, one engagement question, acknowledgements,
and a spoken plan for what happens next -- kept strictly apart from the
**reader** role that answers from the document.

### Two roles, one boundary

| | Companion | Reader |
|---|---|---|
| speaks | progress, invitations, acknowledgements, plans, recaps of *state* | clauses, grounded answers, overview/briefs (enrichment) |
| may see | title, page/section counts, section titles, stage names, the listener's words, ledger state | clause text (retrieved), heard-so-far text, the question |
| may never | state or paraphrase anything the document says; numbers, amounts, %, section citations | small talk |
| source of text | templates first; a model only to *phrase* an acknowledgement or a question | grounding.resolve -> Granite phrasing (unchanged) |
| model | `COMPANION_MODEL` (default = LLM_MODEL, i.e. Granite; `qwen3.5:4b` allowed with think off) | `LLM_MODEL` (Granite) |
| guard | `companion_guard`: reject if output contains a digit, %, currency, "section", "clause", or any 6-word span found in the fixture; on rejection speak the template | existing no-interpretation prompt + eligibility refusal |

Retrieval is not compromised because the companion has no clause text in
its context, cannot cite, and every document claim still comes only from
the reader path with `answer_source` and `retrieval_path` in the trace. A
parked question is *stored*, never answered, until enrichment lands; then it
goes down the reader path like any other question.

### Ambient narration during processing (`Narrator`)

Runs from `api_documents_post` while the sink is claimed, under
`speak_lock`, as `kind: companion` units (acked, interruptible; any listener
reply cancels the line in flight). Sources, in priority order:
  1. **Real progress**, spoken as it happens: "Got the text, {pages} pages."
     (extract) -> "I can see {S} sections: {first three}, and more."
     (structure; the section list is also pushed to the client NOW, before
     enrichment, as `sections_found`) -> "Checking the wording and any
     personal details." (pii_scan) -> "Nearly there, putting an overview
     together." (enrich start) -> "Done."
  2. **One engagement question** ~5 s in: "While I do this, is there anything
     you want to know from it? I'll look for it first." (`ingest_wait`
     prompt; reply parks the question). Acknowledgement on reply: template
     "Okay, I'll keep that in mind: {topic phrase}." where the phrase is the
     `understand()` slot or the reply's first 8 words; with `COMPANION_MODEL`
     set, the model may phrase it (guarded). No reply within 20 s: "Okay. Once
     I'm done I'll give you an overview, and the main sections will show up
     below." -- then no more questions during this ingest.
  3. **Fillers** only when no real event for 12 s, from a rotating script,
     never repeated in a session, max 5 per ingest: "Still working. This one
     is on the longer side." / "About halfway through the sections." (only
     when the stage timings support it) / "Almost there."
Rules: never speak over a prompt that is waiting; a filler never carries a
fact about the document; a real event pre-empts a queued filler; every line
is in the trace as `companion_spoken{source: event|question|ack|plan|filler,
template|model}`; `narration_gap_ms` (longest silence) is reported per
ingest so "no radio silence" is measurable (target p95 < 15 s).

### Ready
Parked question -> "First, what you asked earlier." -> reader path (existing
ask) -> then `start_choice`. No parked question -> `start_choice` as built.
If the listener replied to the engagement question, `start_choice` names it:
"...or shall I start with {topic phrase}?"

The voice for processing (2026-09-07): `take_voice_for(doc, reason, socks,
ws)` on both paths (the upload handler; `_enrich` for an open) -- stop like
an interrupt (`_stop_and_attribute` with reason upload|open), close any prompt
as superseded, "I'll pause here and go through {spoken_title}."; no sink ->
`conv.pending_narration` and `speak_backlog` at the first play. The narrator
plan is in `companion.progress_line` + `Narrator._run`; fillers rotate for as
long as processing takes (no cap). Test: `tests/test_server_narrator.py`.
Chips: `Grounding.topics` guarantees 5-7 (`chip_name`, `section_for_id`);
`opened_message(doc)` carries them; the reducer keeps them for the document
already open.

When `start_choice` is asked (2026-09-07): once per document. Opened from the
rail -> `schedule_start_choice` (1.5 s after the last open,
`START_CHOICE_DEBOUNCE_S`); a page re-opening its current document at load
(no voice, nothing started) -> never; otherwise the first play asks. The
line is "I've gone through {spoken_title}: {S} sections." (no whole-document
minutes). `spoken_title` lives in the index entry (`library.clean_title`,
`ingest.spoken_title_for`; `POST /api/dev/spoken_title`). Side units of the
kinds in `SENTENCE_STREAMED_KINDS` are synthesised one Rime context per
sentence (`_stream_sentences`); `/api/metrics` has `fenced_bytes_session`.

### VRAM note (6 GB)
Granite (2.2 GB) and Qwen 3.5 4B (3.4 GB) do not co-reside with KV caches;
Ollama swaps (~1-2 s each). Enrichment needs Granite in the same window the
companion would use Qwen, so a second model buys little during ingest and
costs a swap per reply after it. Default is one model; `COMPANION_MODEL` is
a switch to try, not the shipped path. Fillers and acknowledgements are
templates precisely so the companion works with no model at all.

## Known issues fixed 2026-09-07

From the project QA findings, in the order fixed (tests named in the table
of the report; all under `tests/`):

1. Narrator only on upload; opening an unenriched fixture spoke one cue then
   fell silent for up to 120 s -> `narrate_enrichment` on the open path,
   lines bound to the enriched document (`test_server_navigator`).
2. One shared enrichment lock and a six-minute rest call starved a later
   document's navigator -> navigator lock with priority, stepwise rest pass
   that yields (`test_enrich_sched`).
3. Text dumps read page markers, "Page x of y", form blanks and checkbox
   glyphs as headings and body -> `clean_lines`, page split, heading rules
   (`test_ingest`).
4. `start_choice` on every `document_opened` -> debounce 1.5 s, never at
   load, else first play (`test_server_start_choice`).
5. Spoken titles were filename stems and UINs -> `spoken_title`
   (`test_ingest`, `test_library`).
6. Whole-document minutes spoken in `start_choice` -> "{S} sections".
7. Duplicate library entries, no delete -> `DELETE /documents/{id}`, text-hash
   dedup with a 409 and a companion line, trash icon (`test_server_delete`).
8. Companion lines attributed to the open document -> `document` on every
   line, prompt and gap report.
9. A Rime cancel left up to 38 s of overview streaming -> one sentence per
   Rime context for side units (`test_server_sentences`).
10. Rest enrichment blocked answers on the shared Ollama -> the rest pass
    yields to answers and prompts; `ENRICH_REST_ENABLED`.
11. Guard rejections not surfaced -> `/dev` table (field, reason, mechanical
    text) and the ingest report view.
12. docx front matter (URLs, phones, UIN/CIN/ISO/"Certified Company" lines)
    read as body -> the `front_matter` rule in the structure pass
    (`test_ingest_structure`).

Second QA pass, trace session_web-8126d423 (same day):

13. The narrator was skipped with `cue_skipped reason=reading` during an
    upload, and the on-open path spoke one cue then nothing for 60 s -> the
    voice takes over for processing on both paths (`take_voice_for`), the
    narration plan runs on both, fillers rotate without a cap, a backlog line
    at the first play when no tab had the voice (`test_server_narrator`).
14. `document_opened` cleared the topic chips of the document being shown,
    and fixtures offered fewer than five chips -> chips carried on
    `document_opened`/`navigator`/`hello`, kept by the reducer for the same
    document, five to seven guaranteed (`test_grounding`, reducer and route
    tests); "Coming up" shows the sections found until the chips land.
15. "Here there's a table of The table is ..." -> the generated description
    as-is; a `table_choice`/`offer` left behind by an open or a jump closes
    `by: navigated`.
16. The duplicate-upload line now reads "That looks like {title}, which is
    already here."; DELETE also broadcasts `library_changed`.
17. Seen in the live pass: a generated suggested question spoke a clause id
    ("mentioned in sec-1-p2") -> `enrich.guard` rejects clause ids
    (`test_enrich`). The pass itself is `scripts/qa_live.py` (fake, then
    rime); it fails on a narration gap over 15 s.

## Rules that are not negotiable

- Heard state is client-acknowledged. Never mark a clause delivered from the
  server's send position.
- Every performance number in README / RIME_EVIDENCE.md traces to a
  committed file under `traces/`. No trace, no number.
- No secrets in source, docs, tests, screenshots, traces or recordings.
  `.env.example` carries placeholders only.
- Rime model / speaker / language must exist in the live catalog at
  submission (`python scripts/fetch_voices.py`).
- No legal or clinical interpretation in answers. Grounded in document text
  or "not covered, ask the insurer/lender".
- English only; browser transport; fixed fixtures. Scope additions must say
  what gets cut.

## Conventions

- Tests pop `LLM_API_KEY` and `LLM_PROVIDER` at import so a sourced `.env`
  cannot route test answers through a live model.
- New events go in the append-only trace with a stable `type`; the /dev page
  is a rendering of that stream, not a second measurement path.
- Prefer small, reversible edits; commit messages name the slice
  (`policy-reader:`, `integrate:`, `evidence:`, `fix:`).
