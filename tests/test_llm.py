"""llm.py: the Ollama answer path. Offline -- requests is monkeypatched."""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))

import llm  # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._p, self.status_code = payload, status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return self._p


ENV_OLLAMA = {"LLM_PROVIDER": "ollama", "LLM_API_KEY": "", "LLM_MODEL": "", "LLM_BASE_URL": ""}


class TestConfig(unittest.TestCase):
    def test_ollama_needs_no_key_and_defaults_to_granite(self):
        with mock.patch.dict(os.environ, ENV_OLLAMA):
            self.assertEqual(llm._config(), ("ollama", "granite4.2:3b", ""))
            self.assertEqual(llm.describe_llm(),
                             {"active": "llm", "provider": "ollama", "model": "granite4.2:3b"})
            self.assertIsNotNone(llm.make_llm())

    def test_anthropic_without_key_is_extractive(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "LLM_API_KEY": ""}):
            self.assertIsNone(llm._config())
            self.assertIsNone(llm.make_llm())
            self.assertEqual(llm.describe_llm()["active"], "extractive")

    def test_base_url_override(self):
        with mock.patch.dict(os.environ, {**ENV_OLLAMA, "LLM_BASE_URL": "http://172.20.0.1:11434/v1/"}):
            self.assertEqual(llm._base_url("ollama"), "http://172.20.0.1:11434/v1")


class TestPost(unittest.TestCase):
    def test_ollama_request_is_deterministic_and_bounded(self):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(url=url, headers=headers, json=json, timeout=timeout)
            return _Resp({"choices": [{"message": {"content": "  Thirty six months.  "}}]})

        with mock.patch.dict(os.environ, {**ENV_OLLAMA, "LLM_TIMEOUT_S": "12", "LLM_MAX_TOKENS": "64"}), \
             mock.patch("requests.post", fake_post):
            out = llm._post("ollama", "", "granite4.2:3b",
                            [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"}])
        self.assertEqual(out, "Thirty six months.")
        self.assertEqual(seen["url"], "http://localhost:11434/v1/chat/completions")
        self.assertNotIn("Authorization", seen["headers"])          # no key, no header
        self.assertEqual(seen["json"]["model"], "granite4.2:3b")
        self.assertEqual(seen["json"]["temperature"], 0)
        self.assertEqual(seen["json"]["max_tokens"], 64)
        self.assertFalse(seen["json"]["stream"])
        self.assertEqual(seen["timeout"], 12.0)
        self.assertEqual([m["role"] for m in seen["json"]["messages"]], ["system", "user"])


class TestCheckAndWarm(unittest.TestCase):
    def test_missing_model_gets_the_pull_command(self):
        def fake_get(url, headers=None, timeout=None):
            return _Resp({"data": [{"id": "llama3.2:3b"}]})
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.get", fake_get):
            c = llm.check_llm()
        self.assertFalse(c["ok"])
        self.assertEqual(c["hint"], "ollama pull granite4.2:3b")

    def test_present_model_is_ok(self):
        def fake_get(url, headers=None, timeout=None):
            self.assertEqual(url, "http://localhost:11434/v1/models")
            return _Resp({"data": [{"id": "granite4.2:3b"}, {"id": "llama3.2:3b"}]})
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.get", fake_get):
            c = llm.check_llm()
        self.assertTrue(c["ok"])
        self.assertIn("2 model(s)", c["detail"])

    def test_unreachable_server_never_raises(self):
        def fake_get(url, headers=None, timeout=None):
            raise ConnectionError("refused")
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.get", fake_get):
            c = llm.check_llm()
        self.assertFalse(c["ok"])
        self.assertIn("unreachable", c["detail"])
        self.assertIn("ollama serve", c["hint"])

    def test_warm_sends_one_token(self):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(json=json)
            return _Resp({"choices": [{"message": {"content": "OK"}}]})
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
            w = llm.warm_llm()
        self.assertTrue(w["ok"])
        self.assertEqual(seen["json"]["max_tokens"], 1)

    def test_nothing_configured(self):
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "LLM_API_KEY": ""}):
            self.assertFalse(llm.check_llm()["ok"])
            self.assertTrue(llm.warm_llm()["ok"])



class TestClassify(unittest.TestCase):
    """Closed-set reply classification: JSON in and out, bounded, and every
    failure is "question" -- the model never invents a navigation decision."""
    OPTIONS = ["a topic name", "the brief", "from the start"]

    def test_valid_json_picks_the_option_and_the_request_is_bounded(self):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(json=json, timeout=timeout)
            return _Resp({"choices": [{"message": {"content": '{"choice": "the brief"}'}}]})

        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
            self.assertEqual(llm.classify("just give me the summary", self.OPTIONS), "the brief")
        self.assertEqual(seen["json"]["max_tokens"], 20)
        self.assertEqual(seen["json"]["temperature"], 0)
        self.assertEqual(seen["timeout"], 5.0)
        user = seen["json"]["messages"][-1]["content"]
        self.assertIn("question", user, "the option set always includes question")
        for o in self.OPTIONS:
            self.assertIn(o, user)

    def test_junk_output_is_a_question(self):
        for content in ("Sure! I think the brief.", '{"choice": "something else"}', "", "{}"):
            with self.subTest(content=content):
                fake_post = lambda *a, **k: _Resp({"choices": [{"message": {"content": content}}]})
                with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
                    self.assertEqual(llm.classify("the bit about claims", self.OPTIONS), "question")

    def test_timeout_is_a_question(self):
        import requests

        def slow_post(*a, **k):
            raise requests.exceptions.Timeout("read timed out")

        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", slow_post):
            self.assertEqual(llm.classify("erm", self.OPTIONS), "question")

    def test_no_model_configured_is_a_question_without_a_request(self):
        boom = mock.Mock(side_effect=AssertionError("must not be called"))
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "LLM_API_KEY": ""}), \
             mock.patch("requests.post", boom):
            self.assertEqual(llm.classify("brief", self.OPTIONS), "question")


class TestMapTopic(unittest.TestCase):
    SECTIONS = [{"id": "sec-4", "title": "General Exclusions"}, {"id": "sec-9", "title": "Claims Procedure"}]

    def test_an_id_from_the_list_is_returned(self):
        fake_post = lambda *a, **k: _Resp({"choices": [{"message": {"content": '{"section": "sec-9"}'}}]})
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
            self.assertEqual(llm.map_topic("how do I make a claim", self.SECTIONS), "sec-9")

    def test_anything_outside_the_list_is_none(self):
        for content in ('{"section": "sec-99"}', '{"section": "none"}', "Claims Procedure", ""):
            with self.subTest(content=content):
                fake_post = lambda *a, **k: _Resp({"choices": [{"message": {"content": content}}]})
                with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
                    self.assertIsNone(llm.map_topic("claims", self.SECTIONS))

    def test_timeout_is_none(self):
        import requests
        slow = mock.Mock(side_effect=requests.exceptions.Timeout("read timed out"))
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", slow):
            self.assertIsNone(llm.map_topic("claims", self.SECTIONS))



class TestUnderstand(unittest.TestCase):
    CTX = {"prompt_kind": "start_choice", "options": ["topic", "brief", "start"],
           "sections": [{"id": "sec-4", "title": "General Exclusions"}, {"id": "sec-9", "title": "Claims Procedure"}],
           "row_labels": ["Cosmetic surgery", "Dental treatment"], "last_heard": None, "reading": False}

    def test_valid_reply_is_returned_with_its_slots_and_the_request_is_bounded(self):
        seen = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            seen.update(json=json, timeout=timeout)
            return _Resp({"choices": [{"message": {"content":
                '{"intent": "topic", "section_id": "sec-4", "row": null, "question": null}'}}]})

        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
            out = llm.understand("something about what is not covered", self.CTX)
        self.assertEqual(out, {"intent": "topic", "section_id": "sec-4", "row": None, "question": None})
        self.assertEqual(seen["json"]["max_tokens"], 80)
        self.assertEqual(seen["json"]["temperature"], 0)
        self.assertEqual(seen["timeout"], 4.0)
        user = seen["json"]["messages"][-1]["content"]
        self.assertIn("sec-9: Claims Procedure", user)
        self.assertIn("Cosmetic surgery", user)
        self.assertIn("OPEN PROMPT: start_choice", user)

    def test_a_question_carries_the_cleaned_question(self):
        fake_post = lambda *a, **k: _Resp({"choices": [{"message": {"content":
            '{"intent": "question", "section_id": null, "row": null, "question": "What is the waiting period?"}'}}]})
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
            out = llm.understand("erm what was the waiting period thing", self.CTX)
        self.assertEqual(out["intent"], "question")
        self.assertEqual(out["question"], "What is the waiting period?")

    def test_an_id_or_label_outside_the_lists_is_dropped_and_the_intent_is_unclear(self):
        for content in ('{"intent": "topic", "section_id": "sec-99", "row": null, "question": null}',
                        '{"intent": "row", "section_id": null, "row": "Hearing aids", "question": null}'):
            with self.subTest(content=content):
                fake_post = lambda *a, **k: _Resp({"choices": [{"message": {"content": content}}]})
                with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
                    self.assertEqual(llm.understand("that one", self.CTX)["intent"], "unclear")

    def test_a_row_label_is_matched_case_insensitively(self):
        fake_post = lambda *a, **k: _Resp({"choices": [{"message": {"content":
            '{"intent": "row", "section_id": null, "row": "dental treatment", "question": null}'}}]})
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
            self.assertEqual(llm.understand("the dental one", self.CTX)["row"], "Dental treatment")

    def test_junk_output_and_unknown_intents_are_unclear(self):
        for content in ("Sure! The user wants exclusions.", '{"intent": "jump", "section_id": "sec-4"}', "", "{}"):
            with self.subTest(content=content):
                fake_post = lambda *a, **k: _Resp({"choices": [{"message": {"content": content}}]})
                with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
                    self.assertEqual(llm.understand("whatever", self.CTX), llm.UNCLEAR)

    def test_timeout_and_no_model_are_unclear(self):
        import requests
        slow = mock.Mock(side_effect=requests.exceptions.Timeout("read timed out"))
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", slow):
            self.assertEqual(llm.understand("exclusions", self.CTX)["intent"], "unclear")
        boom = mock.Mock(side_effect=AssertionError("must not be called"))
        with mock.patch.dict(os.environ, {"LLM_PROVIDER": "anthropic", "LLM_API_KEY": ""}), \
             mock.patch("requests.post", boom):
            self.assertEqual(llm.understand("exclusions", self.CTX)["intent"], "unclear")

    def test_a_recited_system_prompt_is_dropped_from_the_answer(self):
        system = "You are a policy expert with deep knowledge of insurance wordings. Restate the clause plainly."
        echo = "You are a policy expert with deep knowledge of insurance wordings. The clause says the insurer pays hospital costs."
        self.assertEqual(llm.drop_echoed_system(echo, system), "The clause says the insurer pays hospital costs.")
        self.assertEqual(llm.drop_echoed_system("The insurer pays hospital costs.", system), "The insurer pays hospital costs.")
        self.assertEqual(llm.drop_echoed_system("You are covered for hospital costs.", system), "You are covered for hospital costs.")

    def test_an_invented_persona_preamble_is_not_spoken(self):
        self.assertEqual(llm.spoken("You are a policy expert with deep knowledge of insurance. The clause says the insurer pays."),
                         "The clause says the insurer pays.")
        self.assertEqual(llm.spoken("You are covered for hospital costs up to the sum insured."),
                         "You are covered for hospital costs up to the sum insured.")

    def test_a_fabricated_prompt_echo_is_not_spoken(self):
        echo = "You asked: what does that mean\nINSTRUCTIONS: You are a policy expert who restates the clause.\nThe clause says the insurer pays hospital costs."
        self.assertEqual(llm.spoken(echo), "The clause says the insurer pays hospital costs.")
        self.assertEqual(llm.spoken("You asked: what does that mean\nINSTRUCTIONS: You are a policy expert."), "")

    def test_json_callers_do_not_pass_through_the_voice_sanitiser(self):
        # spoken() would strip a leading fragment ending in "?" -- inside JSON that breaks the object.
        fake_post = lambda *a, **k: _Resp({"choices": [{"message": {"content":
            '{"intent": "question", "section_id": null, "row": null, "question": "how much is it?"}'}}]})
        with mock.patch.dict(os.environ, ENV_OLLAMA), mock.patch("requests.post", fake_post):
            self.assertEqual(llm.understand("how much is it", self.CTX)["question"], "how much is it?")


if __name__ == "__main__":
    unittest.main()
