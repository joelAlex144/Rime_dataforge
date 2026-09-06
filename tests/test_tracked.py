"""TrackedTTS: our provider stream converted into the delivery side's events.

The seam is where the two halves' assumptions could quietly disagree, so these
tests pin the four Rime facts the conversion depends on: the audio-sample
anchor, per-segment interleaved timestamps, unbounded stragglers after cancel,
and timings that overshoot the audio.
"""
import asyncio
import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from delivery_layer.events import EventLog                       # noqa: E402
from delivery_layer.normalize import normalize_with_map          # noqa: E402
from delivery_layer.tts.fake import FakeTTS                      # noqa: E402
from delivery_layer.tts.tracked import (                         # noqa: E402
    AudioChunk, Done, Timestamps, TrackedTTS, TrackedUnit,
)


def unit_from(text: str, unit_id: str = "u1", char_start: int = 0) -> TrackedUnit:
    spoken, segs = normalize_with_map(text)
    return TrackedUnit(
        unit_id=unit_id, text_display=text, text_spoken=spoken,
        spoken_map=tuple((s.display_start, s.display_end, s.spoken) for s in segs),
        char_start=char_start,
    )


async def drain(tracked, unit, context_id="ctx1"):
    out = []
    async for ev in tracked.synth(unit, context_id):
        out.append(ev)
    return out


def run(coro):
    # A fresh loop per call: tests/test_server_dev.py uses
    # IsolatedAsyncioTestCase, which closes the global loop, and pytest runs it
    # before this module.
    return asyncio.new_event_loop().run_until_complete(coro)


class TestChunkTiming(unittest.TestCase):
    def setUp(self):
        self.ev = EventLog(None, session_id="t")
        self.t = TrackedTTS(FakeTTS(self.ev), self.ev)

    def test_chunk_spans_are_contiguous_and_sum_to_done_duration(self):
        unit = unit_from("The premium is $1,842.00 per year.")
        evs = run(drain(self.t, unit))
        chunks = [e for e in evs if isinstance(e, AudioChunk)]
        done = [e for e in evs if isinstance(e, Done)][-1]
        self.assertTrue(chunks)
        self.assertEqual(chunks[0].t_start_ms, 0, "anchor is the first audio sample")
        for a, b in zip(chunks, chunks[1:]):
            self.assertEqual(a.t_end_ms, b.t_start_ms, "chunk spans must not gap or overlap")
        self.assertAlmostEqual(chunks[-1].t_end_ms, done.total_duration_ms, delta=1)

    def test_chunk_indices_are_sequential_from_zero(self):
        evs = run(drain(self.t, unit_from("One two three four five six seven.")))
        idx = [e.chunk_index for e in evs if isinstance(e, AudioChunk)]
        self.assertEqual(idx, list(range(len(idx))))

    def test_pcm_is_base64_of_the_raw_bytes(self):
        evs = run(drain(self.t, unit_from("Hello there.")))
        c = [e for e in evs if isinstance(e, AudioChunk)][0]
        raw = base64.b64decode(c.pcm_b64)
        self.assertEqual(len(raw) % 2, 0, "s16le")
        expected_ms = len(raw) / 2 / self.t.sample_rate * 1000
        self.assertAlmostEqual(c.t_end_ms - c.t_start_ms, expected_ms, delta=1)


class TestWordAlignment(unittest.TestCase):
    def setUp(self):
        self.ev = EventLog(None, session_id="t")
        self.t = TrackedTTS(FakeTTS(self.ev), self.ev)

    def test_section_reference_spans_its_three_spoken_tokens(self):
        """"Section 4(b)(ii)" is read as "Section four b two". The WordTiming
        reported back must cover the display span, not the spoken tokens."""
        text = "Water damage is excluded under Section 4(b)(ii) of this policy."
        unit = unit_from(text)
        self.assertIn("four b two", unit.text_spoken)
        evs = run(drain(self.t, unit))
        words = [e for e in evs if isinstance(e, Timestamps)][-1].words

        at = text.index("Section 4(b)(ii)")
        end = at + len("Section 4(b)(ii)")
        covering = [w for w in words if w.char_start < end and w.char_end > at]
        self.assertTrue(covering, "no WordTiming covers the section reference")
        joined = text[min(w.char_start for w in covering):max(w.char_end for w in covering)]
        self.assertIn("4(b)(ii)", joined)

    def test_char_offsets_index_text_display_not_text_spoken(self):
        text = "The deductible is $1,000 per occurrence."
        evs = run(drain(self.t, unit_from(text)))
        for w in [e for e in evs if isinstance(e, Timestamps)][-1].words:
            self.assertLessEqual(w.char_end, len(text))
            self.assertGreaterEqual(w.char_start, 0)

    def test_timestamps_are_cumulative_and_replace_the_previous(self):
        """Rime sends timestamps per segment, so each emission must be the
        whole map so far, never just the newest fragment."""
        evs = run(drain(self.t, unit_from("First sentence here. Second sentence follows.")))
        maps = [e for e in evs if isinstance(e, Timestamps)]
        self.assertTrue(maps)
        for a, b in zip(maps, maps[1:]):
            self.assertGreaterEqual(len(b.words), len(a.words))

    def test_word_times_are_clamped_to_the_audio_on_done(self):
        evs = run(drain(self.t, unit_from("A short clause about water damage.")))
        done = [e for e in evs if isinstance(e, Done)][-1]
        final = [e for e in evs if isinstance(e, Timestamps)][-1]
        self.assertLessEqual(final.words[-1].t_end_ms, done.total_duration_ms + 1,
                             "timings must not run past the audio after clamping")


class TestResumedUnitOffsets(unittest.TestCase):
    def test_char_start_shifts_every_offset_into_original_coordinates(self):
        ev = EventLog(None, session_id="t")
        t = TrackedTTS(FakeTTS(ev), ev)
        tail = "The remainder of the clause continues from here."
        unit = unit_from(tail, unit_id="sec-1/resume#1", char_start=120)
        evs = run(drain(t, unit, "ctx-resume"))
        words = [e for e in evs if isinstance(e, Timestamps)][-1].words
        self.assertTrue(words)
        for w in words:
            self.assertGreaterEqual(w.char_start, 120,
                                    "a resumed unit reports offsets in the original unit")
        self.assertEqual(words[0].char_start, 120)


class TestCancel(unittest.TestCase):
    def test_cancel_yields_no_done_and_exactly_n_fenced(self):
        """At the iterator level cancel ends the stream clean; at the wire
        level the stragglers are counted as result_fenced."""
        for straggle in (1, 3):
            with self.subTest(straggle=straggle):
                ev = EventLog(None, session_id="t")
                t = TrackedTTS(FakeTTS(ev, straggle=straggle), ev)
                unit = unit_from("A fairly long clause so there is more than one chunk to cancel.")

                async def go():
                    seen = []
                    async for e in t.synth(unit, "ctx-cancel"):
                        seen.append(e)
                        if isinstance(e, AudioChunk) and len(seen) >= 2:
                            await t.cancel("ctx-cancel")
                    return seen

                seen = run(go())
                self.assertFalse([e for e in seen if isinstance(e, Done)],
                                 "a cancelled unit must not report Done")
                fenced = [r for r in ev.records if r["type"] == "result_fenced"]
                self.assertEqual(len(fenced), straggle)
                self.assertTrue(all(r.get("bytes", 0) > 0 for r in fenced))

    def test_cancel_nowait_is_callable_from_sync_code(self):
        ev = EventLog(None, session_id="t")
        t = TrackedTTS(FakeTTS(ev), ev)

        async def go():
            t.cancel_nowait("ctx")          # their scheduler calls it like this
            await asyncio.sleep(0)
            return True

        self.assertTrue(run(go()))


class TestPartialTimestampsAtInterrupt(unittest.TestCase):
    def test_boundary_from_a_partial_map_is_conservative(self):
        """At interrupt time the newest map may cover only part of the clause.
        Words with no timing yet cannot be counted as delivered."""
        from delivery_layer.wordmap import WordMap, WordSpan
        text = "First part of the clause. Second part nobody heard."
        partial = WordMap("u", text, [
            WordSpan("First", 0, 200, 0, 5),
            WordSpan("part", 200, 400, 6, 10),
        ])
        # rendered well past the words we have timings for
        self.assertEqual(partial.offset_at(5_000), 10)
        self.assertEqual(partial.heard_text(5_000), "First part")
        self.assertLess(partial.offset_at(5_000), len(text))

    def test_a_straddling_word_is_not_counted_as_heard(self):
        from delivery_layer.wordmap import WordMap, WordSpan
        wm = WordMap("u", "alpha beta", [
            WordSpan("alpha", 0, 300, 0, 5),
            WordSpan("beta", 300, 800, 6, 10),
        ])
        self.assertEqual(wm.offset_at(500), 5, "half-played word is not delivered")


if __name__ == "__main__":
    unittest.main()
