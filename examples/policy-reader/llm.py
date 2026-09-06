"""The optional LLM behind grounded answers. Key lives server-side only.

LLM_API_KEY unset -> make_llm() returns None and answers are extractive (cite
and re-read the clause). Set -> an async callable the grounding module feeds
its no-interpretation prompt. Provider and model from LLM_PROVIDER / LLM_MODEL.
Moved out of chat_demo.py so the web server and the REPL share one client.
"""
from __future__ import annotations

import asyncio
import os


def _post(provider: str, key: str, model: str, messages: list) -> str:
    import requests

    system = "\n".join(m["content"] for m in messages if m["role"] == "system")
    turns = [m for m in messages if m["role"] != "system"]
    if provider == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": model, "max_tokens": 1024, "system": system, "messages": turns},
            timeout=60)
        r.raise_for_status()
        d = r.json()
        if d.get("stop_reason") == "refusal":
            return "I can't answer that one from the document. Ask the insurer or lender."
        return "".join(b.get("text", "") for b in d.get("content", []) if b.get("type") == "text").strip()
    if provider == "openai":
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "content-type": "application/json"},
            json={"model": model, "messages": messages}, timeout=60)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"].strip()
    raise RuntimeError(f"unknown LLM_PROVIDER {provider!r}; use 'anthropic' or 'openai'")


def make_llm():
    """Returns an async llm(messages)->str, or None for extractive mode."""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return None
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    model = os.environ.get("LLM_MODEL", "").strip() or (
        "claude-opus-5" if provider == "anthropic" else "gpt-4o-mini")

    async def llm(messages: list) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _post, provider, key, model, messages)

    return llm


def describe_llm() -> dict:
    """For /api/status: which answer source is active, never the key."""
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        return {"active": "extractive", "provider": None, "model": None}
    provider = os.environ.get("LLM_PROVIDER", "anthropic").strip().lower()
    model = os.environ.get("LLM_MODEL", "").strip() or (
        "claude-opus-5" if provider == "anthropic" else "gpt-4o-mini")
    return {"active": "llm", "provider": provider, "model": model}
