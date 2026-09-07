"""The companion: templates, the guard, and the one guarded model use."""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

import companion as cp  # noqa: E402
import llm  # noqa: E402

HERO = ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"
ENV_OLLAMA = {"LLM_PROVIDER": "ollama", "LLM_API_KEY": "", "LLM_MODEL": "", "LLM_BASE_URL": "", "COMPANION_MODEL": ""}


class _Resp:
    def __init__(self, content):
        self._c, self.status_code = content, 200

    def raise_for_status(self):
        pass

    def json(self):
        return {"choices": [{"message": {"content": self._c}}]}


class TestTemplates(unittest.TestCase):
    def test_progress_lines_per_stage(self):
        self.assertEqual(cp.progress_line("extract", "ok", {"pages": 12}), "Got the text, 12 pages.")
        self.assertEqual(cp.progress_line("extract", "ok", {}), "Got the text.")
        self.assertEqual(cp.progress_line("structure", "ok", {"headings": ["A", "B", "C", "D"], "n_sections": 4}),
                         "I can see 4 sections: A, B, C, and more.")
        self.assertEqual(cp.progress_line("structure", "ok", {"headings": ["A", "B"]}), "I can see 2 sections: A, B.")
        self.assertEqual(cp.progress_line("pii_scan", "ok"), "Checking the wording and any personal details.")
        self.assertEqual(cp.progress_line("enrich", "running"), "Nearly there, putting an overview together.")
        self.assertEqual(cp.progress_line("done", "ok"), "Done.")
        self.assertIsNone(cp.progress_line("segment", "ok"))
        self.assertIsNone(cp.progress_line("extract", "error"))

    def test_topic_phrase_is_the_slot_or_the_first_eight_words(self):
        self.assertEqual(cp.topic_phrase("erm what was the waiting period thing", "What is the waiting period?"),
                         "what is the waiting period")
        self.assertEqual(cp.topic_phrase("one two three four five six seven eight nine ten"),
                         "one two three four five six seven eight")
        self.assertEqual(cp.topic_phrase(""), "that")

    def test_fixed_lines_carry_no_fact(self):
        # Templates are trusted (the guard is for model output), but they must
        # still carry no digit, amount or span of the document. "sections" in a
        # count line is the companion's own vocabulary, not a citation.
        fixture = json.loads(HERO.read_text(encoding="utf-8"))
        for line in cp.FILLERS + [cp.PLAN_LINE, cp.ENGAGEMENT_QUESTION, cp.READY_LINE]:
            self.assertIn(cp.companion_guard(line, fixture), (None, "citation"), line)
            self.assertIsNone(cp._DIGIT.search(line), line)
            self.assertIsNone(cp._CURRENCY.search(line), line)


class TestGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture = json.loads(HERO.read_text(encoding="utf-8"))

    def test_catches_a_number_a_percentage_a_currency_and_a_citation(self):
        self.assertEqual(cp.companion_guard("You have 30 days to claim."), "digit")
        self.assertEqual(cp.companion_guard("It is five percent of the sum."), "currency")
        self.assertEqual(cp.companion_guard("That costs $ ten."), "currency")
        self.assertEqual(cp.companion_guard("Section four says so."), "citation")
        self.assertEqual(cp.companion_guard("As the clause puts it."), "citation")

    def test_catches_a_six_word_span_from_the_fixture(self):
        clause = self.fixture["clauses"][40]["text_display"]
        words = clause.split()
        span = " ".join(words[3:9])
        self.assertEqual(cp.companion_guard(f"Okay, so about {span}, coming up.", self.fixture), "fixture span")
        self.assertIsNone(cp.companion_guard("Okay, I'll keep that in mind: the waiting period.", self.fixture))

    def test_plain_acknowledgements_pass(self):
        self.assertIsNone(cp.companion_guard("Okay, I'll keep that in mind: when the cover starts.", self.fixture))


class TestPhraseAck(unittest.TestCase):
    def test_no_model_configured_is_the_template_and_no_request(self):
        boom = mock.Mock(side_effect=AssertionError("must not be called"))
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "LLM_API_KEY": "", "COMPANION_MODEL": ""}), \
             mock.patch("requests.post", boom):
            self.assertEqual(cp.phrase_ack("what is the waiting period"),
                             ("Okay, I'll keep that in mind: what is the waiting period.", "template"))

    def test_a_good_rewording_is_used(self):
        fake = lambda *a, **k: _Resp("Sure, I will look up what is the waiting period once I am done.")
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake):
            text, origin = cp.phrase_ack("what is the waiting period")
        self.assertEqual(origin, "model")
        self.assertEqual(text, "Sure, I will look up what is the waiting period once I am done.")

    def test_junk_model_output_falls_back_to_the_template(self):
        for content in ("", "Section 4 says the waiting period is 30 days.", "blah " * 30, "Sure thing!", '{"choice": "x"}'):
            with self.subTest(content=content[:20]):
                fake = lambda *a, **k: _Resp(content)
                with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake):
                    text, origin = cp.phrase_ack("what is the waiting period")
                self.assertEqual((text, origin), ("Okay, I'll keep that in mind: what is the waiting period.", "template"))

    def test_a_model_failure_falls_back_to_the_template(self):
        import requests
        slow = mock.Mock(side_effect=requests.exceptions.Timeout("read timed out"))
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", slow):
            self.assertEqual(cp.phrase_ack("what is the waiting period")[1], "template")

    def test_a_qwen_companion_model_sends_think_off(self):
        seen = {}

        def fake(url, headers=None, json=None, timeout=None):
            seen.update(json=json, timeout=timeout)
            return _Resp("Got it, I will check the waiting period for you.")

        with mock.patch.dict(os.environ, {**ENV_OLLAMA, "COMPANION_MODEL": "qwen3.5:4b"}), mock.patch("requests.post", fake):
            text, origin = cp.phrase_ack("what is the waiting period")
        self.assertEqual(origin, "model")
        self.assertEqual(seen["json"]["model"], "qwen3.5:4b")
        self.assertIs(seen["json"]["think"], False)
        self.assertEqual(seen["json"]["reasoning_effort"], "none")
        self.assertEqual(seen["json"]["max_tokens"], 40)
        self.assertEqual(seen["timeout"], 3.0)
        # the answer model's own requests carry no such keys
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake):
            cp.phrase_ack("what is the waiting period")
        self.assertNotIn("think", seen["json"])


if __name__ == "__main__":
    unittest.main()
