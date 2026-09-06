"""
Runnable, no-LiveKit-required demo of the full interruption cycle:
read starts -> listener "interrupts" -> question answered -> reading
resumes at the correct clause.

This does NOT exercise real audio, a real browser, or real Rime -- it
proves the delivery-layer logic (fence + ledger + position + scheduler
+ agent wiring) using tts/fake.py, so it can be run and inspected
without any external services or credentials.

Run:
    python tests/demo_interrupt_cycle.py

Then read traces/demo_ledger.jsonl for the raw event log, or
traces/demo_session_record.json for the derived per-unit delivery
summary -- this is the same kind of artifact the RIME_EVIDENCE.md
acceptance test will be built on top of.
"""

import asyncio
import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

AGENT_PATH = REPO_ROOT / "examples" / "policy-reader" / "agent.py"
FIXTURE_PATH = Path(__file__).resolve().parent / "fixture_demo.json"
LEDGER_PATH = REPO_ROOT / "traces" / "demo_ledger.jsonl"
SESSION_RECORD_PATH = REPO_ROOT / "traces" / "demo_session_record.json"


def _load_agent_module():
    spec = importlib.util.spec_from_file_location("agent", AGENT_PATH)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["agent"] = mod
    spec.loader.exec_module(mod)
    return mod


class DemoSTT:
    """Stands in for real STT -- see agent.py's SpeechToText protocol.
    Not owned by this file; real STT choice is still open."""

    async def transcribe(self, audio_frames) -> str:
        return "what does that mean"


async def main() -> None:
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    if LEDGER_PATH.exists():
        LEDGER_PATH.unlink()

    agent = _load_agent_module()

    published: list[str] = []

    def publish_data(raw: bytes) -> None:
        published.append(raw.decode("utf-8"))

    session = agent.PolicyReaderSession(
        fixture_path=FIXTURE_PATH,
        ledger_path=LEDGER_PATH,
        publish_data=publish_data,
        stt=DemoSTT(),
    )

    print("=== starting read (turn 0): sec-1, sec-2, sec-3 in flight ===")
    read_task = asyncio.create_task(session.start_reading())
    await asyncio.sleep(0.15)  # let synthesis get going before interrupting

    print("=== listener interrupts mid-sentence in sec-1 ===")
    await session.on_speech_start(current_read_unit_id="sec-1", cut_char_offset=20)
    await read_task

    print('=== listener asked: "what does that mean" -> answering + resuming ===')
    await session.on_speech_end(audio_frames=None)

    print()
    print(f"position stack depth after full cycle (expect 0): {session._handles.position_manager.depth}")
    print(f"read cursor after resume (expect 0 -> back at sec-1): {session._handles.read_cursor_order}")

    print()
    print("--- protocol messages that would have gone to the client ---")
    for m in published:
        print(m)

    summary = session._handles.ledger.write_session_record(SESSION_RECORD_PATH)
    print()
    print(f"--- session_record.json written to {SESSION_RECORD_PATH} ---")
    print(f"units: {summary['unit_count']}  heard: {summary['heard']}  "
          f"truncated: {summary['truncated']}  never_played: {summary['never_played']}")
    print()
    print(f"Full raw event ledger: {LEDGER_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
