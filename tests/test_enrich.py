"""Build-time enrichment: guard, verification, idempotency, provider seam.

A fake provider returns canned replies, so the tests exercise exactly what a
model reply goes through -- the parser, the guard, the pointer verification --
without a model. Nothing here touches the network.
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import enrich as en                                                  # noqa: E402
from delivery_layer.enrich.provider import (                         # noqa: E402
    NoneProvider, ProviderError, make_enrich_provider, parse_json_object)

HERO = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"


class FakeProvider:
    """Answers by matching a phrase in the user prompt; records every call."""
    name = "fake"
    model = "fake-1"

    def __init__(self, replies):
        self.replies = replies            # list of (needle, reply) tried in order
        self.calls = []

    def complete(self, system, user, *, max_tokens):
        self.calls.append(user)
        for needle, reply in self.replies:
            if needle in user:
                return reply if not callable(reply) else reply(user)
        return "{}"


def fresh_copy(tmp):
    """The hero fixture with any committed enrichment stripped, so these tests
    exercise generation from a clean document whatever the checked-in state."""
    doc = json.loads(HERO.read_text(encoding="utf-8"))
    for k in ("overview", "sections", "topics", "enrichment"):
        doc.pop(k, None)
    for c in doc["clauses"]:
        for k in ("tags", "spoken_override", "read_inline"):
            c.pop(k, None)
    p = Path(tmp) / "policy.json"
    p.write_text(json.dumps(doc), encoding="utf-8")
    return p


class TestGuard(unittest.TestCase):
    def test_forbidden_phrases(self):
        for bad in ("You should read this", "This is the most IMPORTANT clause", "make sure you claim",
                    "We recommend", "beware of", "the key thing is", "crucial", "you must know", "be careful"):
            self.assertIsNotNone(en.guard(bad), bad)
        self.assertIsNone(en.guard("Lists the events the policy does not cover."))
        self.assertIsNotNone(en.guard("What is the purpose of the fixture document mentioned in sec-1-p2?"))
        self.assertIsNotNone(en.guard("What does clause sec-2-p1 state regarding the purpose of this policy?"))
        self.assertIsNone(en.guard("What does the policy say about the waiting period for cataract treatment?"))
        self.assertIsNone(en.guard("Sets out the procedure for making a claim."))

    def test_parser_takes_the_first_json_object_even_in_a_fence(self):
        self.assertEqual(parse_json_object('```json\n{"a": 1}\n```'), {"a": 1})
        self.assertEqual(parse_json_object('Sure! {"brief": "Covers x."} Hope that helps'), {"brief": "Covers x."})
        with self.assertRaises(ValueError):
            parse_json_object("no json here")


class TestNoneProvider(unittest.TestCase):
    def test_default_is_none_and_generates_nothing(self):
        os.environ.pop("ENRICH_PROVIDER", None)
        p = make_enrich_provider()
        self.assertIsInstance(p, NoneProvider)
        with self.assertRaises(ProviderError):
            p.complete("s", "u", max_tokens=10)
        with tempfile.TemporaryDirectory() as td:
            f = fresh_copy(td)
            before = f.read_bytes()
            info = en.enrich_fixture(f, p, log=open(os.devnull, "w"))
            self.assertEqual(info["fields"], [])
            self.assertEqual(f.read_bytes(), before, "none writes nothing")

    def test_unknown_provider_is_refused(self):
        with self.assertRaises(ProviderError):
            make_enrich_provider("bard")


class TestEnrichFixture(unittest.TestCase):
    def replies(self, brief="Lists the events the policy does not cover."):
        def tags(user):
            ids = [l.split(":", 1)[0] for l in user.splitlines() if l.startswith("sec-")]
            return json.dumps({cid: (["exclusion"] if "not insure" in user else ["amount"]) for cid in ids})
        def questions(user):
            cid = next(l.split(":", 1)[0] for l in user.splitlines() if l.startswith("sec-"))
            return json.dumps({"questions": [
                {"text": "What does the deductible apply to?", "clause_id": cid},
                {"text": "Is there a clause here?", "clause_id": "sec-does-not-exist"},
                {"text": "You should check the limit", "clause_id": cid},
            ]})
        return [
            ("Write 3 to 5 sentences", '{"overview": "This is a homeowners policy issued by Northlake Mutual. It sets out declarations, coverages, definitions and conditions in that order."}'),
            ("Write ONE descriptive line", json.dumps({"brief": brief})),
            ("Tag each clause", tags),
            ("Write 2 or 3 questions", questions),
            ("For each topic below", '{"Exclusions": "General Exclusions", "Definitions": "Definitions", "Claims process": "Conditions Applicable to Property Coverages", "Waiting periods": null, "Premium and charges": "Premium, Renewal and Cancellation", "Cancellation and refunds": "Premium, Renewal and Cancellation", "Grievances and contacts": "Nope Not A Heading"}'),
        ]

    def test_fields_are_generated_marked_and_verified(self):
        p = FakeProvider(self.replies())
        with tempfile.TemporaryDirectory() as td:
            f = fresh_copy(td)
            info = en.enrich_fixture(f, p, log=open(os.devnull, "w"))
            doc = json.loads(f.read_text(encoding="utf-8"))
        self.assertEqual(info["provider"], "fake")
        ov = doc["overview"]
        self.assertTrue(ov["generated"]); self.assertEqual(ov["provider"], "fake")
        self.assertRegex(ov["text"], r"takes about \d+ minutes?\.$")
        self.assertEqual(len(doc["sections"]), 13)
        self.assertTrue(all(s["brief"]["generated"] and s["est_minutes"] >= 1 for s in doc["sections"]))
        # topics: only headings that exist; the last chip is always Read from the start
        chips = doc["topics"]
        self.assertEqual(chips[-1]["topic"], en.READ_FROM_START)
        self.assertNotIn("Grievances and contacts", [c["topic"] for c in chips])     # bogus heading dropped
        self.assertNotIn("Waiting periods", [c["topic"] for c in chips])              # null: no chip
        ids = {c["id"] for c in doc["clauses"]}
        for c in chips[:-1]:
            self.assertIn(c["section_id"], ids)
        # questions: unresolvable pointer dropped, advisory one dropped
        qs = [q for s in doc["sections"] for q in s.get("suggested_questions", [])]
        self.assertTrue(qs)
        for q in qs:
            self.assertIn(q["clause_id"], ids)
            self.assertTrue(q["generated"])
            self.assertIsNone(en.guard(q["text"]))
        self.assertTrue(any(r["field"].startswith("question:") for r in info["guard_rejections"]))
        # tags: subset of the fixed list, marked
        tagged = [c for c in doc["clauses"] if "tags" in c]
        self.assertTrue(tagged)
        for c in tagged:
            self.assertTrue(set(c["tags"]["tags"]) <= set(en.TAGS))
            self.assertTrue(c["tags"]["generated"])
        # clause ids and texts untouched
        hero = json.loads(HERO.read_text(encoding="utf-8"))
        self.assertEqual([c["id"] for c in doc["clauses"]], [c["id"] for c in hero["clauses"]])
        self.assertEqual([c["text_display"] for c in doc["clauses"]], [c["text_display"] for c in hero["clauses"]])

    def test_guard_regenerates_once_then_falls_back_mechanically(self):
        bad = "Beware: this section is the most important one."
        p = FakeProvider([("Write ONE descriptive line", json.dumps({"brief": bad}))] + self.replies()[0:1] + self.replies()[2:])
        with tempfile.TemporaryDirectory() as td:
            f = fresh_copy(td)
            info = en.enrich_fixture(f, p, log=open(os.devnull, "w"))
            doc = json.loads(f.read_text(encoding="utf-8"))
        s0 = doc["sections"][0]
        self.assertRegex(s0["brief"]["text"], r"^Declarations\. \d+ items?\.$", "mechanical fallback")
        rej = [r for r in info["guard_rejections"] if r["field"] == "brief:sec-1-i" or r["field"].startswith("brief:")]
        self.assertTrue(rej)
        self.assertEqual(len(rej[0]["reasons"]), 2, "regenerated once, then fell back")
        # the retry carried the reason
        retries = [c for c in p.calls if "Your previous answer was rejected" in c]
        self.assertTrue(retries)

    def test_idempotent_unless_forced(self):
        p = FakeProvider(self.replies())
        with tempfile.TemporaryDirectory() as td:
            f = fresh_copy(td)
            en.enrich_fixture(f, p, log=open(os.devnull, "w"))
            n = len(p.calls)
            info = en.enrich_fixture(f, p, log=open(os.devnull, "w"))
            self.assertEqual(len(p.calls), n, "second run makes no model calls")
            self.assertEqual(info["fields"], [])
            en.enrich_fixture(f, p, force=True, log=open(os.devnull, "w"))
            self.assertGreater(len(p.calls), n)

    def test_listening_time_is_computed_not_generated(self):
        cps, src = en.measured_chars_per_second()
        self.assertGreater(cps, 5.0)
        self.assertLess(cps, 40.0)
        self.assertEqual(en.spoken_minutes(int(cps * 60 * 3), cps), 3)


if __name__ == "__main__":
    unittest.main()
