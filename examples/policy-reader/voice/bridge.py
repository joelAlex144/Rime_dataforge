#!/usr/bin/env python3
"""Voice-input bridge: LiveKit room -> VAD + STT -> the existing /ws/audio protocol.

This is the ONLY new piece of runtime logic voice input adds. It does not
import, subclass, or modify anything in delivery_layer/ or server.py. It
talks to the running server exactly the way the browser's text UI already
does: it opens a normal WebSocket connection to /ws/audio and sends

    {"type": "interrupt"}                       # on VAD speech-start
    {"type": "ask", "question": "<transcript>"} # on STT final transcript

`handle_client_message` in server.py already knows how to do the right
thing with both of those messages -- flush the sink's audio clock, cancel
synthesis, attribute the delivery boundary, resolve the question against
the last-heard clause, speak the answer, and resume. None of that changes.

What this file owns, and nothing else:
  - joining the per-session LiveKit room as a subscriber-only bot
  - running Silero VAD on the listener's published mic track to decide
    *when* a barge-in happened (this is the interrupt trigger)
  - turning the isolated utterance into text once VAD says the turn ended

STT is a single REST call per utterance to an OpenAI-compatible
/audio/transcriptions endpoint -- Groq by default (whisper-large-v3-turbo;
fast, cheap, and on its own quota separate from OPENAI_API_KEY), OpenAI or
another compatible provider if STT_BASE_URL/STT_MODEL/STT_API_KEY are set.
This is called directly rather than through livekit-agents' streaming STT
plugin, which only works against OpenAI's *realtime* transcription
websocket -- that finalizes a turn from *trailing silence in a continuous
stream*, and has nothing to detect once our own VAD has already isolated a
silence-free utterance and handed it over as a single closed clip. A
one-shot REST call matches what we actually have (one complete utterance,
cut to size by our own VAD) and finalizes immediately, with no server-side
turn detector to mistune.

Run one instance per server process (it discovers the session id from the
server itself, since a demo runs one session at a time):

    python examples/policy-reader/voice/bridge.py --server http://127.0.0.1:8080

Requires LIVEKIT_URL, LIVEKIT_API_KEY, LIVEKIT_API_SECRET, and one of
GROQ_API_KEY / OPENAI_API_KEY / STT_API_KEY, plus whatever RIME_API_KEY the
server itself already needs (this process does not touch TTS at all).
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urljoin

import aiohttp
import openai
from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import vad as vad_base
from livekit.plugins import silero

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # examples/policy-reader
from voice.livekit_token import mint_token, room_name_for_session  # noqa: E402

log = logging.getLogger("voice-bridge")

MIN_UTTERANCE_MS = 200  # shorter than this is almost always a VAD false trigger, not speech

# STT is one REST call to an OpenAI-compatible /audio/transcriptions endpoint
# (see the module docstring for why REST, not the streaming plugin). Groq
# serves the same Whisper-family models over the same request/response shape
# as OpenAI's own endpoint, so this is a config swap, not a different code
# path -- default to Groq (fast, cheap, and doesn't share a quota with
# whatever else in this project uses OPENAI_API_KEY); override any of the
# three to point at OpenAI or another OpenAI-compatible provider instead.
STT_BASE_URL = os.environ.get("STT_BASE_URL", "https://api.groq.com/openai/v1")
STT_MODEL = os.environ.get("STT_MODEL", "whisper-large-v3-turbo")
STT_API_KEY = (
    os.environ.get("STT_API_KEY")
    or os.environ.get("GROQ_API_KEY")
    or os.environ.get("OPENAI_API_KEY")
)


async def _fetch_session_id(server_http: str) -> str:
    async with aiohttp.ClientSession() as http:
        async with http.get(urljoin(server_http, "/api/status")) as resp:
            data = await resp.json()
            return data["session_id"]


class ServerLink:
    """The bridge's only channel back to the reader: a plain client on
    /ws/audio, identical in kind to the browser tab's own connection.

    Reconnects on its own if the connection drops. Without this, one lost
    connection (server restart, a network blip, anything) permanently
    breaks every interrupt/ask for the rest of the bridge's life -- the
    process looks alive (VAD keeps firing) but every send silently throws
    into a dead socket forever."""

    def __init__(self, server_ws_url: str) -> None:
        self._url = server_ws_url
        self._ws: aiohttp.ClientWebSocketResponse | None = None
        self._session: aiohttp.ClientSession | None = None
        self._reconnect_lock = asyncio.Lock()

    async def connect(self) -> None:
        self._session = aiohttp.ClientSession()
        self._ws = await self._session.ws_connect(self._url, heartbeat=20)
        log.info("connected to reader at %s", self._url)

    async def _ensure_connected(self, *, force: bool = False) -> None:
        if not force and self._ws is not None and not self._ws.closed:
            return
        async with self._reconnect_lock:
            # An abrupt reset (what we're actually seeing) doesn't always
            # flip ws.closed to True, so a forced reconnect must not be
            # skipped by this check the way a lazy one is -- only a
            # concurrent call that already reconnected excuses skipping it.
            if not force and self._ws is not None and not self._ws.closed:
                return
            log.warning("connection to reader is down, reconnecting to %s", self._url)
            if self._session is not None:
                await self._session.close()
            await self.connect()

    async def _send(self, payload: dict) -> None:
        await self._ensure_connected()
        assert self._ws is not None
        try:
            await self._ws.send_str(json.dumps(payload))
        except (ConnectionError, aiohttp.ClientError):
            # One retry against a forced-fresh connection -- if the reader
            # really is down (not just a stale socket), this second attempt
            # fails too and the caller's own try/except logs it as before.
            await self._ensure_connected(force=True)
            assert self._ws is not None
            await self._ws.send_str(json.dumps(payload))

    async def send_interrupt(self) -> None:
        await self._send({"type": "interrupt"})
        log.info("-> interrupt")

    async def send_ask(self, question: str) -> None:
        await self._send({"type": "ask", "question": question})
        log.info("-> ask %r", question)

    async def close(self) -> None:
        if self._ws is not None:
            await self._ws.close()
        if self._session is not None:
            await self._session.close()


async def _transcribe_utterance(
    openai_client: openai.AsyncOpenAI,
    frames: list[rtc.AudioFrame],
    link: ServerLink,
) -> None:
    """One batch STT call per utterance: VADEvent.END_OF_SPEECH already hands
    us the complete, silence-trimmed speech segment as a list of frames --
    combine them into one clip and transcribe it in a single REST call."""
    if not frames:
        return
    duration_ms = sum(f.samples_per_channel / f.sample_rate for f in frames) * 1000
    if duration_ms < MIN_UTTERANCE_MS:
        log.info("utterance too short (%.0f ms), skipping STT", duration_ms)
        return

    combined = rtc.combine_audio_frames(frames)
    wav_bytes = combined.to_wav_bytes()

    try:
        resp = await openai_client.audio.transcriptions.create(
            model=STT_MODEL,
            file=("utterance.wav", io.BytesIO(wav_bytes), "audio/wav"),
        )
    except Exception:
        log.exception("STT request failed")
        return

    text = (resp.text or "").strip()
    if not text:
        log.info("STT returned no text for this utterance")
        return
    try:
        await link.send_ask(text)
    except Exception:
        log.exception("failed to send ask (is the server still up at this URL?)")


async def _handle_track(
    track: rtc.RemoteAudioTrack,
    vad_engine: vad_base.VAD,
    openai_client: openai.AsyncOpenAI,
    link: ServerLink,
) -> None:
    audio_stream = rtc.AudioStream(track)
    vad_stream = vad_engine.stream()

    async def _pump_vad() -> None:
        async for evt in audio_stream:
            vad_stream.push_frame(evt.frame)

    async def _consume_vad_events() -> None:
        async for ev in vad_stream:
            if ev.type == vad_base.VADEventType.START_OF_SPEECH:
                log.info("VAD: speech start (barge-in)")
                try:
                    await link.send_interrupt()
                except Exception:
                    # A dead connection to the server must not kill this loop --
                    # if it did, every VAD event after the first failure would
                    # be silently dropped with no sign anything was wrong.
                    log.exception("failed to send interrupt (is the server still up at this URL?)")
            elif ev.type == vad_base.VADEventType.END_OF_SPEECH:
                log.info("VAD: speech end, %d frames -> STT", len(ev.frames))
                asyncio.create_task(_transcribe_utterance(openai_client, ev.frames, link))

    await asyncio.gather(_pump_vad(), _consume_vad_events())


async def run(args: argparse.Namespace) -> None:
    load_dotenv()
    logging.basicConfig(level=logging.INFO)

    server_http = args.server.rstrip("/")
    server_ws = server_http.replace("http://", "ws://").replace("https://", "wss://") + "/ws/audio"

    session_id = args.session_id or await _fetch_session_id(server_http)
    room_name = room_name_for_session(session_id)
    token = mint_token(session_id, "voice-bridge", can_publish=False, can_subscribe=True)

    link = ServerLink(server_ws)
    await link.connect()

    vad_engine = silero.VAD.load()
    if not STT_API_KEY:
        raise RuntimeError(
            "no STT key set: provide STT_API_KEY, GROQ_API_KEY, or OPENAI_API_KEY"
        )
    openai_client = openai.AsyncOpenAI(api_key=STT_API_KEY, base_url=STT_BASE_URL)

    room = rtc.Room()
    track_tasks: list[asyncio.Task] = []

    @room.on("track_subscribed")
    def _on_track(track: rtc.Track, publication, participant):  # noqa: ANN001
        if track.kind == rtc.TrackKind.KIND_AUDIO:
            log.info("subscribed to audio from %s", participant.identity)
            t = asyncio.create_task(_handle_track(track, vad_engine, openai_client, link))
            track_tasks.append(t)

    livekit_url = os.environ["LIVEKIT_URL"]
    await room.connect(livekit_url, token)
    log.info("voice bridge connected to room %s, waiting for the listener's mic", room_name)

    try:
        await asyncio.Event().wait()
    finally:
        for t in track_tasks:
            t.cancel()
        await room.disconnect()
        await link.close()
        await openai_client.close()


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--server", default="http://127.0.0.1:8080",
                    help="base HTTP URL of the running policy-reader server")
    p.add_argument("--session-id", default=None,
                    help="override auto-discovered session id")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
