"""Word map alignment: Rime tokens -> display chars, including '4(b)(ii)' <-> 'four b two'."""
import unittest

from delivery_layer.normalize import normalize_with_map
from delivery_layer.wordmap import build_word_map


def fake_timestamps(spoken: str, ms_per_word=300.0, drop=None, merge=None):
    """Synthesise Rime-shaped timestamps from the spoken string.
    drop: index of a token Rime 'forgot'; merge: (i, j) tokens Rime emitted as one."""
    words = spoken.split()
    if merge:
        i, j = merge
        words = words[:i] + [" ".join(words[i:j])] + words[j:]
    if drop is not None:
        words = words[:drop] + words[drop + 1:]
    start = [k * ms_per_word for k in range(len(words))]
    end = [s + ms_per_word for s in start]
    return words, start, end


class TestWordMap(unittest.TestCase):
    TEXT = "Water damage under Section 4(b)(ii) is limited to $10,000, effective January 1, 2026."

    def build(self, **kw):
        spoken, segs = normalize_with_map(self.TEXT)
        w, s, e = fake_timestamps(spoken, **kw)
        return build_word_map("u1", self.TEXT, segs, w, s, e), spoken

    def test_spans_cover_text_and_are_monotonic(self):
        wm, _ = self.build()
        self.assertEqual(wm.spans[0].char_start, 0)
        self.assertEqual(wm.spans[-1].char_end, len(self.TEXT))
        for a, b in zip(wm.spans, wm.spans[1:]):
            self.assertLessEqual(a.char_end, b.char_start)
            self.assertLessEqual(a.t_end_ms, b.t_start_ms + 1e-6)
        self.assertFalse(any(s.estimated for s in wm.spans))

    def test_section_ref_maps_three_tokens_to_one_span(self):
        wm, spoken = self.build()
        span = next(s for s in wm.spans if "4(b)(ii)" in s.word)
        toks = spoken.split()
        i_four, i_two = toks.index("Section"), toks.index("two")
        self.assertAlmostEqual(span.t_start_ms, i_four * 300.0)
        self.assertAlmostEqual(span.t_end_ms, (i_two + 1) * 300.0)
        self.assertEqual(span.word, "Section 4(b)(ii)")

    def test_boundary_mid_word_is_conservative(self):
        wm, _ = self.build()
        span = next(s for s in wm.spans if "4(b)(ii)" in s.word)
        mid = (span.t_start_ms + span.t_end_ms) / 2
        # Half-way through "four b two": the section ref is NOT heard yet.
        self.assertEqual(wm.heard_text(mid), self.TEXT[: span.char_start].rstrip())
        self.assertEqual(wm.heard_text(span.t_end_ms), self.TEXT[: span.char_end])
        self.assertEqual(wm.heard_text(0), "")
        self.assertEqual(wm.heard_text(10 ** 9), self.TEXT)

    def test_punctuation_attaches_to_previous_word(self):
        wm, _ = self.build()
        span = next(s for s in wm.spans if s.word.startswith("$10,000"))
        self.assertEqual(span.word, "$10,000,")  # comma merged

    def test_tolerates_dropped_and_merged_tokens(self):
        wm, spoken = self.build(drop=3)
        self.assertEqual(wm.spans[-1].char_end, len(self.TEXT))
        self.assertLessEqual(sum(s.estimated for s in wm.spans), 1)
        wm2, _ = self.build(merge=(0, 2))
        self.assertEqual(wm2.spans[-1].char_end, len(self.TEXT))
        for a, b in zip(wm2.spans, wm2.spans[1:]):
            self.assertLessEqual(a.t_end_ms, b.t_start_ms + 1e-6)


if __name__ == "__main__":
    unittest.main()
