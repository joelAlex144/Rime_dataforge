"""heard is client-acknowledged, never server-estimated.

unit_ended is the client's definitive completion signal. The server accepts it
only when the client's frame count matches the bytes it sent (a shortfall is a
dropped chunk, not a heard unit) and it records the rendered_ms the client
carries on that message -- the client's own drained count. It never fills in
audio_ms itself: an earlier revision did, and it was reverted because it
stamped a delivery number the client had not reported.
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "examples" / "policy-reader"))         # noqa: E402
import server as srv                                                 # noqa: E402

FRAMES = 4800                     # 200 ms at 24 kHz
BYTES = FRAMES * 2
AUDIO_MS = 200.0
LAST_ACK_MS = 125.0               # the periodic ack lands up to 100 ms short


class HeardCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.s = srv.ReaderSession(dev=False)
        doc = self.s.library.current
        self.unit_id = next(iter(doc.grounding.by_id))
        self.cid = f"{self.unit_id}#t1"
        self.st = srv.ContextState(context_id=self.cid, turn_id=1, unit_id=self.unit_id,
                                   state="playing", bytes=BYTES, rendered_ms=LAST_ACK_MS,
                                   audio_ms=AUDIO_MS, synth_done=True)
        self.s.contexts[self.cid] = self.st

    async def asyncTearDown(self):
        p = getattr(self.s.events, "path", None)
        if p and Path(p).exists():
            Path(p).unlink()

    async def ended(self, **fields):
        m = {"type": "unit_ended", "context_id": self.cid, **fields}
        await srv.handle_client_message(self.s, m, set(), None)

    def heard_events(self):
        return self.s.events.of_type("unit_heard")

    def mismatches(self):
        return self.s.events.of_type("frame_count_mismatch")


class TestUnitEndedAccepted(HeardCase):
    async def test_records_the_rendered_ms_the_client_sent(self):
        await self.ended(enqueued_frames=FRAMES, rendered_ms=AUDIO_MS)
        self.assertTrue(self.st.heard)
        self.assertEqual(self.st.rendered_ms, AUDIO_MS)
        self.assertEqual(self.heard_events()[0]["rendered_ms"], AUDIO_MS)
        self.assertEqual(self.mismatches(), [])

    async def test_the_final_value_is_logged_as_a_client_frames_played_ack(self):
        await self.ended(enqueued_frames=FRAMES, rendered_ms=AUDIO_MS)
        finals = [e for e in self.s.events.of_type("frames_played") if e.get("final")]
        self.assertEqual(len(finals), 1)
        self.assertEqual(finals[0]["rendered_ms"], AUDIO_MS)

    async def test_server_never_fills_in_audio_ms_when_the_client_sends_no_value(self):
        # Frames match, so the unit is heard -- but rendered_ms stays at what
        # the client last reported. Stamping audio_ms here was the reverted bug.
        await self.ended(enqueued_frames=FRAMES)
        self.assertTrue(self.st.heard)
        self.assertEqual(self.st.rendered_ms, LAST_ACK_MS)
        self.assertEqual(self.heard_events()[0]["rendered_ms"], LAST_ACK_MS)

    async def test_client_value_never_moves_rendered_ms_backwards(self):
        await self.ended(enqueued_frames=FRAMES, rendered_ms=50.0)
        self.assertEqual(self.st.rendered_ms, LAST_ACK_MS)


class TestUnitEndedRefused(HeardCase):
    async def test_one_dropped_chunk_is_a_mismatch_not_a_heard_unit(self):
        # 512 frames short: exactly one lost 1024-byte Rime block.
        await self.ended(enqueued_frames=FRAMES - 512, rendered_ms=AUDIO_MS)
        self.assertFalse(self.st.heard)
        self.assertEqual(self.heard_events(), [])
        mm = self.mismatches()
        self.assertEqual(len(mm), 1)
        self.assertEqual(mm[0]["via"], "unit_ended")
        self.assertEqual(mm[0]["enqueued_frames"], FRAMES - 512)
        self.assertEqual(mm[0]["expected_frames"], FRAMES)
        # A refused report carries no weight: its rendered_ms is not recorded.
        self.assertEqual(self.st.rendered_ms, LAST_ACK_MS)

    async def test_missing_frame_count_is_refused(self):
        await self.ended(rendered_ms=AUDIO_MS)
        self.assertFalse(self.st.heard)
        self.assertEqual(self.mismatches()[0]["enqueued_frames"], None)

    async def test_ignored_until_the_provider_has_finished_sending(self):
        self.st.synth_done = False
        await self.ended(enqueued_frames=FRAMES, rendered_ms=AUDIO_MS)
        self.assertFalse(self.st.heard)
        self.assertEqual(self.mismatches(), [])
        self.assertEqual(self.st.rendered_ms, LAST_ACK_MS)


if __name__ == "__main__":
    unittest.main()
