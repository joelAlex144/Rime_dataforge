# Fixtures

Every fixture here is a committed JSON file in the schema `build_fixture.py`
established. Nothing in the agent path fetches or parses a document at runtime —
these files are the input.

**A fixture is not committed without a human running `--review` and reading the
clause list.** The segmenter's failure modes are silent: a dropped introducer, a
heading absorbed into a clause, or site chrome read aloud as policy text all
validate cleanly. The eyeball step is the only thing that catches them.

---

## `policy.json` — the hero document

| | |
|---|---|
| Source | **Synthetic.** No real insurer, insured, or policy text. |
| Built by | `python examples/policy-reader/fixtures/build_fixture.py` |
| Clauses | 213, across 11 sections |
| Numbering | Explicit — `sec-4b-ii` style ids, `section` / `subsection` / `item` all populated |
| Known problems | None. It is generated, so extraction cannot fail. |

Frozen. `build_fixture.py` and `policy.json` are not modified by the ingestion
pipeline, and `scripts/ingest.py` never writes to this file.

---

## `carers_allowance.json` — the ingested second document

| | |
|---|---|
| Source | <https://www.gov.uk/carers-allowance/eligibility> (Crown copyright, Open Government Licence v3.0) |
| Fetched | 2026-09-05T17:34:38Z |
| sha256 (raw extracted text) | `2ac3b3491b00ca01587598e5f8923ea7e29d21b800949ab7ce9b5f7d9a12ba63` |
| Clauses | 45, across 9 sections |
| Numbering | **None** — headings become sections, paragraphs become clauses, ids are `sec-<n>-p<k>` |
| Reviewed | yes, `--review`; two splitter bugs found and fixed before committing |

```bash
python scripts/ingest.py "https://www.gov.uk/carers-allowance/eligibility" \
  --out examples/policy-reader/fixtures/carers_allowance.json \
  --title "Carer's Allowance: eligibility (GOV.UK)" --review
```

Chosen because it is plain server-rendered HTML (no login, no JS rendering), it
is unnumbered — so it exercises the rule-2 path that `policy.json` never touches
— and it is a benefits eligibility page, which is exactly the shape of document
that makes the eligibility refusal load-bearing.

### Known extraction problems

- **Introducer borrowing is inconsistent by design.** Rule 5 prepends the
  introducing sentence only to bullets under six words, so within one list
  `sec-3-p4` reads *"The person you care for must already get one of these
  benefits: Attendance Allowance"* while `sec-3-p1` is the bare *"Personal
  Independence Payment - daily living component"* (seven words, so it stands
  alone). Both are intelligible; the list is not internally uniform.
- **`sec-7-p8` swallows a one-word heading.** The source has a standalone
  `Example` paragraph before the worked example. At 7 characters it is under
  `--min-clause-chars`, so rule 4 merges it into the following clause, giving
  *"Example You earn £100 a week…"*. Correct per the merge rule, slightly odd
  read aloud.
- **Site chrome needed an explicit block list.** The first run ingested GOV.UK's
  "Is this page useful?" / "Help us improve GOV.UK" feedback widget as policy
  text. `extract_html` now decomposes elements whose class or id matches
  `feedback|survey|improve|share|related|contents-list|print-link|pagination|search|subscri`.
  A new source that leaks furniture needs that list extended — and a note here.
- **Rates go stale.** The page quotes £204/week earnings and £86.45/week pension
  figures current at the fetch date. The fixture is a snapshot, not a live
  reading of the scheme. `source.fetched_at` and `source.sha256` are how you tell.
- **The refusal wording is insurance-flavoured.** `ELIGIBILITY_REFUSAL` ends
  "contact the insurer or lender", which reads oddly on a benefits page where the
  right referral is DWP. The sentence is fixed verbatim by the brief, so it is
  unchanged; a per-fixture referral string would be the fix.

---

## Adding another fixture

1. `python scripts/ingest.py <path-or-url> --dry-run` and read the output.
2. Fix what is wrong **in the segmentation rules**, not in the JSON. A
   hand-edited fixture cannot be regenerated and will drift.
3. Re-run with `--review`, read the clause list, press Enter.
4. Add a row and a "known extraction problems" list here.
5. `python -m pytest -q` — the schema and validator tests must still pass.

Refusals you may hit: the PII scan exits 2 on emails, phone numbers, street
addresses, or a capitalised name next to a 9+ digit number. Use a synthetic or
public document. `--allow-pii "reason"` exists for false positives only, and the
reason is written into `source.pii_override_reason` where a reviewer will see it.
