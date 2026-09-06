"""
Runnable, no-LiveKit-required demo of the full interruption cycle:
read starts -> listener "interrupts" -> question answered -> reading
resumes at the correct clause.

This does NOT exercise a real browser. It runs against the real fixture
(examples/policy-reader/fixtures/policy.json) through the real provider --
TTS_PROVIDER=fake for no key, TTS_PROVIDER=rime for the real voice -- and
stands a SimulatedClient in for the AudioWorklet.

That stand-in matters. Delivery truth comes only from client acks, so with
nothing playing the audio every unit correctly resolves as never_played and no
boundary can ever be computed. SimulatedClient plays the role the worklet plays
in the browser: it consumes the published protocol messages, counts what it
"rendered", acks every 100 ms, and on a Cancel reports where audio actually
stopped. It never tells the ledger anything the server already knew.

Run:
    TTS_PROVIDER=fake python tests/demo_interrupt_cycle.py
    TTS_PROVIDER=rime python tests/demo_interrupt_cycle.py    # needs .env

Then read traces/demo_ledger.jsonl for the raw event log, or
traces/demo_session_record.json for the derived per-unit delivery summary.
"""

import asyncio
import importlib.util
import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "examples" / "policy-reader"))

os.environ.setdefault("FAKE_REALTIME", "1")   # pace the fake so a cut lands mid-unit

from delivery_layer.playback_protocol import decode          # noqa: E402

AGENT_PATH = REPO_ROOT / "examples" / "policy-reader" / "agent.py"
FIXTURE_PATH = REPO_ROOT / "examples" / "policy-reader" / "fixtures" / "policy.json"
LEDGER_PATH = REPO_ROOT / "traces" / "demo_ledger.jsonl"
SESSION_RECORD_PATH = REPO_ROOT / "traces" / "demo_session_record.json"

ACK_EVERY_MS = 100
RESUME_CHAR_START = {}
DEMO_UNITS = 3      # a window; the fixture has 213 clauses


def _load_agent_module():
    spec = importlib.util.spec_from_file_location("agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent"] = mod
    spec.loader.exec_module(mod)
    return mod


class DemoSTT:
    """Stands in for real STT -- see agent.py's SpeechToText protocol."""

    async def transcribe(self, audio_frames) -> str:
        return "what does that mean"


class SimulatedClient:
    """Stands in for client/worklet/playback-processor.js.

    Counts rendered ms from the chunk spans it receives and acks every
    ACK_EVERY_MS, the cadence the worklet uses. On Cancel it flushes and
    reports the position it had actually reached -- that number, not anything
    the server knew, is what the ledger turns into a character boundary.
    """

    def __init__(self):
        self.session = None
        self.turn_of = {}
        self.stopped_at = None
        # A single playhead over units in arrival order, like the worklet's
        # queue. The scheduler keeps 2 units in flight, so chunks for the next
        # unit arrive while this one is still sounding; whichever unit_start
        # came last is NOT what the listener is hearing, and attributing the
        # cut to it would put the boundary on the wrong clause.
        self._queue = []          # [unit_id, ...] in arrival order
        self._buffered_ms = {}    # unit_id -> ms of audio received
        self._rendered_ms = {}    # unit_id -> ms actually "played"
        self._last_ack = {}
        self._playing = True

    @property
    def head(self):
        """The unit the listener is actually hearing."""
        for u in self._queue:
            if self._rendered_ms.get(u, 0.0) < self._buffered_ms.get(u, 0.0):
                return u
        return self._queue[-1] if self._queue else None

    def publish(self, raw: bytes) -> None:
        msg = decode(raw.decode("utf-8"))
        kind = msg.type.value
        if kind == "unit_start":
            if msg.unit_id not in self._queue:
                self._queue.append(msg.unit_id)
            self._buffered_ms.setdefault(msg.unit_id, 0.0)
            self._rendered_ms.setdefault(msg.unit_id, 0.0)
            self.turn_of[msg.unit_id] = msg.turn_id
        elif kind == "audio_chunk":
            self._buffered_ms[msg.unit_id] = float(msg.t_end_ms)
            self.turn_of[msg.unit_id] = msg.turn_id
        elif kind == "cancel":
            self._playing = False
            u = self.head
            if u is not None and self._rendered_ms.get(u):
                at = self._rendered_ms[u]
                self.stopped_at = (u, at)
                self.session.on_flush_ack(
                    unit_id=u, turn_id=self.turn_of.get(u, msg.turn_id),
                    rendered_ms=int(at),
                    audible_stop_ts=asyncio.get_event_loop().time())

    async def play(self) -> None:
        """Advance the playhead in real time and ack every ACK_EVERY_MS, the
        cadence the worklet uses. Only the head unit renders; the rest is
        buffered, exactly as in the browser."""
        step = ACK_EVERY_MS / 1000.0
        while self._playing:
            await asyncio.sleep(step)
            u = self.head
            if u is None:
                continue
            avail = self._buffered_ms.get(u, 0.0)
            if self._rendered_ms.get(u, 0.0) >= avail:
                continue
            self._rendered_ms[u] = min(avail, self._rendered_ms.get(u, 0.0) + ACK_EVERY_MS)
            if self._rendered_ms[u] - self._last_ack.get(u, 0.0) >= ACK_EVERY_MS:
                self._last_ack[u] = self._rendered_ms[u]
                self.session.on_playback_ack(
                    unit_id=u, turn_id=self.turn_of.get(u, 0),
                    rendered_ms=int(self._rendered_ms[u]))


def pick_cut(units):
    """A clause with at least two sentences, cut inside the second one.

    A single-sentence clause always resumes at char 0, which would not show
    whether mid-unit resume works at all.
    """
    for u in units:
        if len(u.sentences) >= 2:
            s2 = u.sentences[1]
            return u, int(s2[0] + (s2[1] - s2[0]) * 0.4)
    u = units[0]
    return u, len(u.text_display) // 2


async def main() -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LEDGER_PATH.exists():
        LEDGER_PATH.unlink()

    agent = _load_agent_module()
    client = SimulatedClient()

    units = agent.load_units(FIXTURE_PATH)
    target, cut_at = pick_cut(units)
    start_order = target.order

    session = agent.PolicyReaderSession(
        fixture_path=FIXTURE_PATH,
        ledger_path=LEDGER_PATH,
        publish_data=client.publish,
        stt=DemoSTT(),
    )
    client.session = session
    h = session._handles
    # Read a three-clause window, not all 213. FAKE_REALTIME paces synthesis at
    # wall-clock speed so the interrupt can land mid-unit, which would otherwise
    # mean waiting for the entire policy to be read aloud.
    window = [u for u in h.units if start_order <= u.order < start_order + DEMO_UNITS]
    h.units = window
    h.units_by_id = {u.unit_id: u for u in window}
    h.read_cursor_order = 0
    h.answer_provider = agent.GroundingAnswerProvider(
        FIXTURE_PATH,
        cursor_fn=lambda: h.read_cursor_order,
        heard_text_fn=lambda: None,
    )

    print(f"provider: {os.environ.get('TTS_PROVIDER', 'rime')}   fixture: {FIXTURE_PATH.name}")
    print(f"=== starting read at {target.unit_id} ({target.clause_label}) ===")
    play_task = asyncio.create_task(client.play())
    read_task = asyncio.create_task(session.start_reading())

    # Interrupt once the listener is genuinely past the first sentence, rather
    # than after a fixed sleep. A cut inside sentence 0 resumes at char 0 and
    # would not show whether mid-unit resume works at all; this waits for the
    # real acked boundary to cross the sentence, so char_start > 0 is earned.
    first_sentence_end = target.sentences[0][1]
    deadline = asyncio.get_event_loop().time() + 40
    while asyncio.get_event_loop().time() < deadline:
        if h.ledger.delivered_char_end(target.unit_id) > first_sentence_end:
            break
        await asyncio.sleep(0.1)
    heard_chars = h.ledger.delivered_char_end(target.unit_id)
    print(f"    (acked boundary reached char {heard_chars}; "
          f"sentence 0 ends at {first_sentence_end})")

    print(f"=== listener interrupts inside {target.unit_id}, past sentence 0 ===")
    await session.on_speech_start(current_read_unit_id=target.unit_id, cut_char_offset=cut_at)
    await read_task

    print('=== listener asked: "what does that mean" -> answering + resuming ===')
    await session.on_speech_end(audio_frames=None)

    print()
    print(f"position stack depth after full cycle (expect 0): {h.position_manager.depth}")
    print(f"read cursor after resume (expect 1, i.e. the NEXT unit, not a replay): "
          f"{h.read_cursor_order}")
    if client.stopped_at:
        print(f"client stopped at: {client.stopped_at[0]} @ {client.stopped_at[1]:.0f} ms")

    client._playing = False
    play_task.cancel()
    RESUME_CHAR_START.update(
        {rid: cs for rid, (_orig, cs) in getattr(h.ledger, "_resumed", {}).items()})
    summary = h.ledger.write_session_record(SESSION_RECORD_PATH)
    print()
    print(f"units: {summary['unit_count']}  heard: {summary['heard']}  "
          f"truncated: {summary['truncated']}  never_played: {summary['never_played']}")

    rows = [json.loads(l) for l in LEDGER_PATH.read_text().splitlines() if l.strip()]
    checks(rows, summary)
    print()
    print(f"Full raw event ledger: {LEDGER_PATH}")


def checks(rows, summary) -> None:
    """The STEP 5 assertions, printed rather than asserted so a run always
    shows the whole picture instead of stopping at the first miss."""
    print()
    print("--- checks ---")

    pa = [r for r in rows if r["type"] == "provider_active"]
    detail = ("/".join(str(pa[0].get(k)) for k in
                       ("provider", "modelId", "speaker", "lang", "audioFormat", "samplingRate"))
              if pa else "none")
    print(f"  [{'ok' if len(pa) == 1 else 'XX'}] exactly one provider_active: {len(pa)}  {detail}")

    trunc = [r for r in rows if r["type"] == "unit_truncated"]
    unit = next((u for u in summary["units"] if u["status"] == "truncated_at_offset"), None)
    print(f"  [{'ok' if trunc else 'XX'}] unit_truncated events: {len(trunc)}"
          + (f"  rendered_ms={trunc[0]['rendered_ms']}" if trunc else ""))
    if unit:
        print(f"       boundary from the word map: {len(unit['delivered_text'])} chars"
              f" of {unit['unit_id']}, last word index {unit['boundary_word_index']},"
              f" straddling={unit['straddling_word']!r}")

    resumed = [u for u in summary["units"] if "/resume#" in u["unit_id"]]
    cs = RESUME_CHAR_START.get(resumed[0]["unit_id"]) if resumed else None
    print(f"  [{'ok' if resumed and cs else 'XX'}] resumed unit with char_start > 0: "
          + (f"{resumed[0]['unit_id']} char_start={cs}" if resumed else "none"))

    saved = [r for r in rows if r["type"] == "position_saved"]
    restored = [r for r in rows if r["type"] == "position_restored"]
    print(f"  [{'ok' if saved and restored else 'XX'}] position_saved/restored: "
          f"{len(saved)}/{len(restored)}")

    fenced = [r for r in rows if r["type"] == "result_fenced"]
    fenced_bytes = sum(int(r.get("bytes") or 0) for r in fenced)
    print(f"  [--] result_fenced: {len(fenced)} events, {fenced_bytes} bytes after cancel")

    frames = [r for r in rows if r["type"] == "frames_played"]
    print(f"  [{'ok' if frames else 'XX'}] frames_played acks (delivery truth): {len(frames)}")
    heard_without_acks = [
        u for u in summary["units"]
        if u["status"] == "heard"
        and not any(r["type"] == "frames_played" and r.get("unit_id") == u["unit_id"]
                    for r in rows)
        and "/resume#" not in u["unit_id"]
    ]
    print(f"  [{'ok' if not heard_without_acks else 'XX'}] A5 no unit heard without acks: "
          f"{len(heard_without_acks)} violations")


if __name__ == "__main__":
    asyncio.run(main())
