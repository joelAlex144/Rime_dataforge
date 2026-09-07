"""One interface, three providers, selected by ENRICH_PROVIDER.

  ollama  local. OLLAMA_BASE_URL (default http://localhost:11434), OLLAMA_MODEL
          (default granite4.2:3b, the same local model the runtime answers use). /api/chat, temperature 0.
  groq    hosted, OpenAI-compatible at https://api.groq.com/openai/v1, for a
          machine that cannot run a local model. GROQ_API_KEY from the
          environment, GROQ_MODEL from the environment with NO default: the
          catalogue changes, so at startup the model list is fetched and an
          unset or unknown model prints the available instruct models and
          exits. Free tier: ~30 requests/min and ~6,000 tokens/min, so callers
          batch, honour Retry-After on 429 with exponential backoff, and a
          running token count is printed.
  none    no model. Every enrichment field stays empty; the reader falls back
          to the mechanical map. Always works.

Both model providers take identical prompts and return raw text; parsing is
the caller's and identical for both, so switching provider changes quality,
never schema. Credentials are read here, server-side, and nowhere else.
"""
from __future__ import annotations

import json
import os
import sys
import time
from typing import Optional, Protocol


class ProviderError(RuntimeError):
    pass


class EnrichProvider(Protocol):
    name: str

    def complete(self, system: str, user: str, *, max_tokens: int) -> str: ...


class NoneProvider:
    name = "none"
    model = None

    def complete(self, system: str, user: str, *, max_tokens: int) -> str:
        raise ProviderError("ENRICH_PROVIDER=none: nothing is generated")


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None) -> None:
        self.base_url = (base_url or os.environ.get("OLLAMA_BASE_URL") or "http://localhost:11434").rstrip("/")
        self.model = model or os.environ.get("OLLAMA_MODEL") or "granite4.2:3b"
        self.tokens_in = 0
        self.tokens_out = 0

    def complete(self, system: str, user: str, *, max_tokens: int) -> str:
        import requests
        r = requests.post(f"{self.base_url}/api/chat", timeout=300, json={
            "model": self.model, "stream": False,
            "options": {"temperature": 0, "num_predict": int(max_tokens)},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        })
        if r.status_code != 200:
            raise ProviderError(f"ollama {r.status_code}: {r.text[:200]}")
        d = r.json()
        self.tokens_in += int(d.get("prompt_eval_count") or 0)
        self.tokens_out += int(d.get("eval_count") or 0)
        return (d.get("message") or {}).get("content", "") or ""


class GroqProvider:
    name = "groq"
    BASE_URL = "https://api.groq.com/openai/v1"

    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 verify_model: bool = True, stream=sys.stderr) -> None:
        try:
            from openai import OpenAI
        except ImportError as e:
            raise ProviderError("the groq provider needs the openai client: "
                                "pip install -r requirements-build.txt") from e
        key = api_key or os.environ.get("GROQ_API_KEY", "").strip()
        if not key:
            raise ProviderError("GROQ_API_KEY is not set (server-side .env only)")
        self.client = OpenAI(api_key=key, base_url=self.BASE_URL)
        self.model = model or os.environ.get("GROQ_MODEL", "").strip()
        self.tokens_in = 0
        self.tokens_out = 0
        self.requests = 0
        self._stream = stream
        if verify_model:
            self._check_model()

    def _check_model(self) -> None:
        """No model name lives in source. Fetch the catalogue; refuse to guess."""
        try:
            listed = [m.id for m in self.client.models.list().data]
        except Exception as e:
            raise ProviderError(f"could not list Groq models: {e}") from e
        if self.model and self.model in listed:
            return
        instruct = sorted(m for m in listed if not any(x in m.lower() for x in ("whisper", "tts", "guard", "embed")))
        print("GROQ_MODEL is " + ("unset" if not self.model else f"{self.model!r}, not in the catalogue")
              + ". Available instruct models:", file=self._stream)
        for m in instruct:
            print(f"  {m}", file=self._stream)
        raise SystemExit(2)

    def complete(self, system: str, user: str, *, max_tokens: int) -> str:
        delay = 2.0
        for attempt in range(6):
            try:
                resp = self.client.chat.completions.create(
                    model=self.model, temperature=0, max_tokens=int(max_tokens),
                    messages=[{"role": "system", "content": system}, {"role": "user", "content": user}])
            except Exception as e:                       # 429 and friends
                status = getattr(e, "status_code", None)
                if status == 429 or "rate limit" in str(e).lower():
                    retry_after = None
                    headers = getattr(getattr(e, "response", None), "headers", None)
                    if headers is not None:
                        try:
                            retry_after = float(headers.get("retry-after"))
                        except (TypeError, ValueError):
                            retry_after = None
                    wait = retry_after if retry_after else delay
                    print(f"groq 429: waiting {wait:.0f} s (attempt {attempt + 1})", file=self._stream)
                    time.sleep(wait)
                    delay = min(delay * 2, 60)
                    continue
                raise ProviderError(f"groq: {e}") from e
            self.requests += 1
            u = getattr(resp, "usage", None)
            if u is not None:
                self.tokens_in += int(getattr(u, "prompt_tokens", 0) or 0)
                self.tokens_out += int(getattr(u, "completion_tokens", 0) or 0)
            print(f"groq: {self.requests} requests, {self.tokens_in} in / {self.tokens_out} out tokens",
                  file=self._stream)
            return resp.choices[0].message.content or ""
        raise ProviderError("groq: rate limited six times in a row; try again later")


def make_enrich_provider(name: Optional[str] = None, **kw):
    """ENRICH_PROVIDER=ollama|groq|none (default none)."""
    chosen = (name or os.environ.get("ENRICH_PROVIDER", "none")).strip().lower()
    if chosen in ("", "none", "off"):
        return NoneProvider()
    if chosen == "ollama":
        return OllamaProvider(**kw)
    if chosen == "groq":
        return GroqProvider(**kw)
    raise ProviderError(f"unknown ENRICH_PROVIDER {chosen!r}; use ollama, groq or none")


def parse_json_object(text: str) -> dict:
    """The one parser both model providers' output goes through: the first
    {...} block in the reply, tolerant of code fences."""
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(t[start:end + 1])
