"""playback_protocol round-trips, including the v2 char_start field.

char_start is what lets the client show a resumed fragment at its real place
inside the clause instead of at character 0, so it has to survive the wire and
it has to default correctly when an older message arrives without it.
"""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delivery_layer.playback_protocol import (          # noqa: E402
    PROTOCOL_VERSION, AudioChunk, Cancel, ClientError, FlushAck, PlaybackAck,
    UnitDone, UnitStart, WordSpan, WordTimestamps, decode, encode,
)


class TestRoundTrip(unittest.TestCase):
    def assert_round_trip(self, msg):
        back = decode(encode(msg))
        self.assertEqual(back, msg)
        return back

    def test_every_message_type_round_trips(self):
        for msg in [
            UnitStart(turn_id=1, unit_id="sec-1-i", seq=0, sample_rate_hz=24000, channels=1),
            AudioChunk(turn_id=1, unit_id="sec-1-i", seq=0, chunk_index=0,
                       pcm_b64="AAAA", t_start_ms=0, t_end_ms=80),
            WordTimestamps(turn_id=1, unit_id="sec-1-i",
                           words=(WordSpan("Hello", 0, 180, 0, 5),)),
            UnitDone(turn_id=1, unit_id="sec-1-i", total_duration_ms=2560),
            Cancel(turn_id=2, unit_id=None),
            PlaybackAck(turn_id=1, unit_id="sec-1-i", rendered_ms=800, client_clock_ts=1.5),
            FlushAck(turn_id=2, unit_id="sec-1-i", rendered_ms=2560, audible_stop_ts=3.25),
            ClientError(turn_id=1, unit_id=None, message="boom"),
        ]:
            with self.subTest(type(msg).__name__):
                self.assert_round_trip(msg)


class TestCharStart(unittest.TestCase):
    def test_version_is_two(self):
        self.assertEqual(PROTOCOL_VERSION, 2)

    def test_char_start_defaults_to_zero_for_a_normal_unit(self):
        m = UnitStart(turn_id=0, unit_id="sec-1-i", seq=0, sample_rate_hz=24000, channels=1)
        self.assertEqual(m.char_start, 0)
        self.assertEqual(decode(encode(m)).char_start, 0)

    def test_char_start_survives_the_wire_for_a_resumed_unit(self):
        m = UnitStart(turn_id=3, unit_id="sec-4b-vii/resume#3", seq=0,
                      sample_rate_hz=24000, channels=1, char_start=120)
        self.assertEqual(decode(encode(m)).char_start, 120)
        self.assertEqual(json.loads(encode(m))["char_start"], 120)

    def test_encoded_message_carries_the_new_version(self):
        m = UnitStart(turn_id=0, unit_id="u", seq=0, sample_rate_hz=24000, channels=1)
        self.assertEqual(json.loads(encode(m))["version"], 2)

    def test_a_version_1_unit_start_still_decodes(self):
        """Older committed traces predate char_start; 0 is the right reading."""
        v1 = json.dumps({"type": "unit_start", "version": 1, "turn_id": 0,
                         "unit_id": "sec-1-i", "seq": 0,
                         "sample_rate_hz": 24000, "channels": 1})
        m = decode(v1)
        self.assertEqual(m.char_start, 0)
        self.assertEqual(m.unit_id, "sec-1-i")


class TestAnchorConvention(unittest.TestCase):
    def test_module_documents_the_audio_sample_anchor(self):
        """The original convention said t=0 was synth_requested. Both halves
        now agree it is the first audio sample; if that line is ever reverted
        the ledger's boundary comparison silently drifts."""
        import delivery_layer.playback_protocol as pp
        doc = pp.__doc__ or ""
        self.assertIn("FIRST AUDIO SAMPLE", doc)
        self.assertNotIn("t=0 == synth_requested, not first-byte", doc)


if __name__ == "__main__":
    unittest.main()
