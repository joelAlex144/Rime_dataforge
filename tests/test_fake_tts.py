"""Stream contract + fencing on the fake provider (same contract Rime must meet)."""
import asyncio
import unittest

from delivery_layer.events import EventLog
from delivery_layer.tts import FakeTTS
from delivery_layer.tts.base import AudioChunk, Done, Timestamps


class TestFakeTTS(unittest.TestCase):
    def test_stream_contract(self):
        async def go():
            ev = EventLog()
            tts = FakeTTS(ev, ms_per_word=100, chunk_ms=50)
            await tts.connect()
            items = [i async for i in tts.synth("one two three four", "c1")]
            return ev, items
        ev, items = asyncio.run(go())
        self.assertEqual(ev.of_type("provider_active")[0]["provider"], "fake")
        self.assertIsInstance(items[0], Timestamps)
        self.assertIsInstance(items[-1], Done)
        pcm = sum(len(i.pcm) for i in items if isinstance(i, AudioChunk))
        self.assertEqual(pcm, items[-1].total_bytes)
        self.assertAlmostEqual(pcm / 2 / 24000 * 1000, 400.0, delta=1)

    def test_cancel_fences_and_ends_without_done(self):
        async def go():
            ev = EventLog()
            tts = FakeTTS(ev, ms_per_word=100, chunk_ms=20, realtime=True)
            await tts.connect()
            got = []
            async for i in tts.synth("one two three four five six", "c1"):
                got.append(i)
                if isinstance(i, AudioChunk) and i.seq == 2:
                    await tts.cancel()
            return ev, got
        ev, got = asyncio.run(go())
        self.assertFalse(any(isinstance(i, Done) for i in got))
        self.assertEqual(len(ev.of_type("cancel_issued")), 1)
        self.assertGreaterEqual(len(ev.of_type("result_fenced")), 1)


if __name__ == "__main__":
    unittest.main()
